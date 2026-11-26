# Native lock hierarchy: low-level design

<!-- cspell:ignore contextmanager contextvar ctypes wintypes inodes -->
<!-- cspell:ignore argtypes restype LPOVERLAPPED dpkg Putzolu Traiger pids -->

Status: draft. Builds on the accepted [FRD](./frd.md) and the [HLA](./hla.md). Code citations are
`file:line` under `cli/agentworks/` at 27b215e9b unless a path says otherwise.

This document fixes the primitive, the one sequence every operation follows, the holder records, the
lock set of every command, the mechanism for each piece of shared state, the tests and the
collateral, and [section 8](#8-delivery-two-stacked-prs) splits the implementation into two stacked
PRs. [Appendix A](#appendix-a-implementation-checklist) maps each current site to its change, by PR.

## 1. The primitive

### Package

A new package `native_locks/` replaces `harness_setup/locking.py` (`native_mutation_guard`,
`NativeMutationGuard`, `NativeSetupBusyError`) in one step. The lock serves every native mutation,
not harness setup, so it moves out of `harness_setup` and its names stop saying "setup".

| Module         | Holds                                                                                                                                       |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `__init__.py`  | `LockMode`, `EntityKind`, `Entity`, `LockRequest`, `NativeLockSet`, `native_locks`, `probe_locks`, `NativeLockBusyError`, `operation_label` |
| `_entities.py` | entity constructors and ancestor closure                                                                                                    |
| `_os.py`       | the platform calls: open, try-lock shared or exclusive, unlock                                                                              |
| `_records.py`  | holder records and the holder lookup a refusal uses                                                                                         |
| `rows.py`      | entity builders from database rows (`session_entity_for(db, row)` and the like)                                                             |

### Entities

```python
class EntityKind(StrEnum):  # declaration order is the global lock order
    VM = "vm"
    WORKSPACE = "workspace"
    ACCOUNT = "account"
    GRANT_ALL = "grant-all"
    PATH = "path"
    GRANT = "grant"
    SESSION = "session"
    CONSOLE = "console"


@dataclass(frozen=True)
class Entity:
    vm: str
    kind: EntityKind
    name: str
    parents: tuple[Entity, ...] = field(compare=False)
```

Identity is `(vm, kind, name)`. Entities are built only through constructors, and `__post_init__`
enforces the tree: a vm has no parents and its name equals `vm`; a workspace, account, grant-all or
console has the vm among its parents; a path's parent is the path of its parent directory, or the vm
for a top-level directory; a session's parents are exactly one workspace and one account; a grant's
are one workspace and one account; a console's are the vm and its member sessions; every parent
belongs to the same vm.

| Constructor                                  | Display key                     | Notes                                              |
| -------------------------------------------- | ------------------------------- | -------------------------------------------------- |
| `vm_entity(vm)`                              | `vm/<vm>`                       | the existing vm file                               |
| `workspace_entity(vm, name)`                 | `workspace/<name>`              |                                                    |
| `account_entity(vm, linux_user)`             | `account/<linux user>`          | `AgentRow.linux_user` or `VMRow.admin_username`    |
| `grant_all_entity(vm)`                       | `grant-all`                     | the VM's set of grant-all agents                   |
| `path_entity(vm, resolved_path)`             | `path/<resolved absolute path>` | one directory tree on the VM, by its resolved path |
| `grant_entity(workspace, account)`           | `grant/<workspace>+<account>`   | one account's membership in one workspace group    |
| `session_entity(name, workspace=, account=)` | `session/<name>`                |                                                    |
| `console_entity(vm, name, members=)`         | `console/<name>`                | members are session entities                       |

Accounts are keyed by Linux user rather than agent name because the account is the thing on the VM
(`agents/manager/_common.py:109` derives `agt-<name>` at create; legacy rows keep their stored
user), and the admin and every agent are distinct users on one VM, so no key collides.

`grant-all` names a set no other entity owns. A grant-all agent must hold a grant for every
workspace on its VM (`agents/grants.py:296`). Workspace create reads the set of grant-all agents
(`workspaces/realize.py:209`), and agent create with `--grant-all-workspaces`, grant `--all` and
revoke `--all` change that set and then read the set of workspaces (`agents/realize.py:225-230`,
`agents/grants.py:104-110`, `:176-185`). Without an entity for that set, a workspace create and a
grant-all agent create on one VM could each miss the other. Readers hold `grant-all` shared, writers
exclusive.

A `path` entity names a directory on the VM by a physical absolute path the guest reports, so path
locks follow the same two-mode rule as everything else. Every name comes from the guest's lookup
walk ([Workspace paths, section 4](#rows-that-need-more-than-a-line)), never from workstation
normalization, which is unsafe across links and refuses the `..` that stored legacy paths keep on
purpose (`workspaces/guest.py:51-70`). An operation that creates, fills, removes or recursively
reads a workspace directory tree takes the walk's final path exclusive (for a delete, the entry it
removes) and every directory the walk looked a name up in shared: every directory the kernel
consults to reach the tree, including those reached between symbolic-link expansions, so moving any
of them is refused while the operation runs. Each symbolic link the walk follows is also taken
shared at its own entry (its directory joined with its name), so an unlink-only delete of that link,
which takes the entry exclusive, is refused while the operation runs. A path entity's parent is the
path of its parent directory (its string prefix), or the vm for a top-level directory, so the tree
also adds each locked directory's prefixes shared. `/` is excluded: every path operation would hold
it shared and none can hold it exclusive (a workspace path of `/` is refused at config validation),
so it would never decide anything. Because a path's prefixes are its ancestors, the `(kind, name)`
order still takes them first. Two operations on disjoint trees share only shared directories and
proceed together; an operation inside a tree holds the tree's root shared, so an operation removing,
filling or recursively reading that root (which holds it exclusive) is refused, and the reverse. An
in-flight target is covered by its own exclusive lock until its operation ends; no pending record is
needed.

### Modes and requests

```python
class LockMode(IntEnum):
    SHARED = 1
    EXCLUSIVE = 2


@dataclass(frozen=True)
class LockRequest:
    exclusive: tuple[Entity, ...] = ()
    shared: tuple[Entity, ...] = ()

    def modes(self) -> Mapping[Entity, LockMode]: ...
```

`modes()` gives every entity in `exclusive` the exclusive mode and every other entity in `shared` or
in the ancestor closure of either set the shared mode. All entities of one request name one VM, and
the constructor refuses otherwise; a command that spans VMs holds one set per VM. A request whose
only entity is the vm, shared, is an access command's admission request. The request takes tuples,
not sets, because `Entity` equality ignores parents: `__post_init__` walks every entity given and
every ancestor reached, refuses two that share a key but name different parents (two reads that
disagree), and only then builds the keyed mode map.

Two modes conflict unless both are shared. An operation therefore conflicts with another exactly
when one changes an entity the other holds in any mode, which is R2: a change below holds its
ancestors shared and so refuses a change above, a change above holds its entity exclusive and so
refuses anything below, and unrelated subtrees share only shared ancestors.

### Order

Entities sort by `(kind order, name)` with the kind order of `EntityKind`. Every parent kind
precedes its children's kinds (vm before all; workspace and account before grant and session;
session before console), so an acquisition always takes ancestors before descendants and a refusal
at a parent touches nothing below it. Sets on several VMs are acquired in VM name order, as
`runtime_state/_selection.py:98` does today.

The order also means two contending acquisitions cannot both be refused. If A is refused at `a`
(held by B) and B at `b` (held by A), then A took `b` before reaching `a`, so `b < a`, and
symmetrically `a < b`, a contradiction.

### Acquire, probe, require, release

```python
@contextmanager
def native_locks(
    database_path: Path, request: LockRequest, *, prompts: bool = False
) -> Iterator[NativeLockSet]: ...


def probe_locks(database_path: Path, request: LockRequest) -> None: ...


class NativeLockSet:
    database_path: Path
    vm: str

    def covers(self, entity: Entity, mode: LockMode) -> bool: ...
    def require(self, request: LockRequest, *, database_path: Path) -> None: ...
```

- **Acquire**: each acquisition takes its whole request, opening each file of `request.modes()` in
  order and trying its mode without waiting. The first refusal releases everything this call took,
  then raises `NativeLockBusyError`. On success the set writes its holder record
  ([section 3](#3-holder-records)) and is yielded; on exit it retires the record first, then
  releases every entity. A set never grows: every operation builds its whole request from a read
  made before locking ([section 4](#rules)), and a runtime family's retry may drop targets, never
  add one ([Runtime families](#runtime-families)).
- **Probe**: the same acquisition released at once, writing no record, over every entity of the
  request except `path` entities, whose names exist only after the guest resolves them (step 5) and
  which join the request at acquisition. It lets a command fail on a conflict before it prompts or
  boots; it decides nothing, since the real acquisition follows.
- **Require** (nested reuse, replacing today's `held=`): a nested operation states the request for
  what it changes and calls `held.require(request, database_path=db.path)` with its own
  `Database.path`; the set compares it resolved, and the request's VM, with its own, as
  `harness_setup/locking.py:45-48` does today, and checks it is still active. It then checks
  coverage: an entity is covered in mode `m` when the set holds it in mode `m` or stronger, or when
  the set holds the vm exclusive (nothing else can hold anything on the VM then). An exclusive on
  any other ancestor does not cover: a grant or session has two parents, and exclusive on one says
  nothing about the other. That is how `vm create` and `vm reinit` cover the VM facet's setup and
  the admin account's setup under the vm exclusive. Coverage compares keys and modes, never parents,
  so a nested console delete after a session's membership cascade still matches. Missing coverage
  raises `StateError`, an interior contract. A nested operation never opens a second handle on an
  entity its caller holds: within one process two handles conflict on both platforms.
- There is no upgrade: neither `flock` conversion (documented as non-atomic, `flock(2)`) nor
  `LockFileEx` (which cannot lock exclusive over its own shared range) upgrades safely, so each
  request names the strongest mode it will need.
- A set's entity map is fixed at acquisition, so the threads of the teardown coordinator
  (`orchestration/README.md:188-193`) read it without coordination. Only the owning thread releases.

`NativeLockBusyError(StateError)` carries `entity_kind` and `entity_name` of the refused entity, the
refused mode, and `holder: HolderInfo | None` (the record it named). Tests and machine output read
those fields; the message is for people.

### Files

```text
<db dir>/.<db name>.native-locks/              0700, as today
    <sha256(vm)>                               vm lock: unchanged path, one byte
    <sha256(vm)>.d/                            0700
        workspace-<sha256(name)>
        account-<sha256(linux user)>
        grant-all
        grant-<sha256(workspace + "\0" + account)>
        session-<sha256(name)>
        console-<sha256(name)>
        path-<sha256(resolved path)>
        holders/<16 hex>.json                  holder records
```

Every lock file is opened as today (`harness_setup/locking.py:55-60`: `a+b`, write one `\0` when
empty, seek to 0), so each has a stable byte 0 on Windows. Files are created on first use and never
unlinked, for the reason the current docstring gives (`locking.py:42-43`): unlinking could let two
processes lock different inodes. So lock files accumulate, one one-byte file per entity name ever
locked on a VM, and a `vm delete` leaves its `<sha256(vm)>.d/` behind, since another process may
still hold one of its files open. That is acceptable: the count grows only with the names an
operator creates, each file is one byte in the operator's own state directory, and correctness would
cost more than the space saved.

Names are hashed so any entity name is a safe file name. Two creates of one name on one VM lock the
same file before the row exists, so the second is refused. Creates of one name on different VMs lock
different files and are left to the database's primary keys (`db/migrations.py:741`, `:538`, `:634`,
`:669`), as today.

### Platform calls

POSIX (Linux and macOS): `fcntl.flock(fd, LOCK_SH | LOCK_NB)` or `LOCK_EX | LOCK_NB`; refusal is
`errno.EWOULDBLOCK` only (checked here: two descriptions of one file conflict for SH/EX, EX/SH and
EX/EX and not for SH/SH); release `LOCK_UN`, then close.

Windows, through `ctypes` because `msvcrt.locking` has no shared mode:

```python
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


_LPOVERLAPPED = ctypes.POINTER(_OVERLAPPED)
kernel32.LockFileEx.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, _LPOVERLAPPED
)
kernel32.LockFileEx.restype = wintypes.BOOL
kernel32.UnlockFileEx.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, _LPOVERLAPPED
)
kernel32.UnlockFileEx.restype = wintypes.BOOL

LOCKFILE_FAIL_IMMEDIATELY = 0x1
LOCKFILE_EXCLUSIVE_LOCK = 0x2
ERROR_LOCK_VIOLATION = 33
```

- Lock: `LockFileEx(msvcrt.get_osfhandle(fd), flags, 0, 1, 0, byref(overlapped))` with offset 0 and
  `flags = LOCKFILE_FAIL_IMMEDIATELY | (LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0)`. A `FALSE`
  return with `ctypes.get_last_error() == ERROR_LOCK_VIOLATION` is a refusal; any other error raises
  `ctypes.WinError`. The handle is synchronous (Python's `open`), so the call returns at once.
- Unlock: `UnlockFileEx(handle, 0, 1, 0, byref(overlapped))` on the same byte, then close. Unlocking
  before closing matters on Windows, where locks left at close are released "depending upon
  available system resources" (LockFileEx documentation).
- Both modes lock the same one-byte range at offset 0, the byte today's code locks.

### Interlock with an older agw

Every release that has the lock (it is unchanged since v0.19.0, commit be88ad65e) takes the vm file
exclusive and knows nothing below it. Every new lock set contains the vm entity, at least shared,
because it is the ancestor of everything. So:

- An old holder (exclusive on the vm file) refuses every new acquisition on that VM, whose first
  file is the vm file. The new process prints the fallback message (no record names the old holder).
- A new holder (shared or exclusive on the vm file) refuses an old acquisition, which prints today's
  message.
- The interlock covers only what the old release guarded. Bodies an older release leaves unguarded
  (before PR 1, agent revoke, copy's source, `vm import-host-keys` and `vm confirm-release`; before
  the guards #956 added, also grant, workspace repair and copy's destination) take no lock, so an
  older process running one of them is not excluded from anything a new process does, as it is not
  excluded from anything today; PR 1's guards protect only processes running PR 1 or later.

On POSIX this is `flock` on the same inode: the path derivation (`database_path.resolve()`, the
directory name, `sha256(vm name)`) is kept exactly. On Windows the old code calls
`msvcrt.locking(fd, LK_NBLCK, 1)` after `seek(0)` (`locking.py:60`, `:84`); the CRT's `_locking`
implements that as `LockFile` on one byte at the file position, which is an exclusive lock.
`LockFileEx` documents that "exclusive locks cannot overlap an existing locked region" and that
"shared locks can overlap a locked region provided locks held on that region are shared locks", so
an old `LockFile` and a new shared or exclusive `LockFileEx` on byte 0 refuse each other in both
orders (the old side's failure is `EACCES`, which `locking.py:64` already maps to busy). I verified
the POSIX side by running the four mode pairs; the Windows side rests on the documented semantics
above and is proved by the Windows-marked interlock test in [section 6](#6-tests), which must pass
before merge.

### Crash release

The OS releases every lock when the holder exits or is killed: `flock` locks belong to the open file
description, closed at exit; Windows releases a terminated process's byte-range locks (possibly
after a short delay, which the tests tolerate by polling for a bounded few seconds). Lock files are
opened non-inheritable (Python's default, PEP 446) and agw starts subprocesses with `close_fds`, so
a surviving child such as an `ssh` process cannot keep a lock alive; the implementation must not
pass these descriptors to children or `fork` without `exec`. A crashed holder leaves its holder
record behind; [Stale records](#stale-records) removes it.

### Why nothing deadlocks

No acquisition of the native lock waits: every call fails immediately, and a process that waits for
nothing cannot be part of a wait cycle. The only bounded waits the design touches are leaf critical
sections that acquire no native lock while held: the SSH config rebuild mutex
([section 5](#ssh-config-rebuild)), shadow-utils' own `lckpwdf`, dpkg's lock and the guest file
program's lock (`execution/file_program.py:41-45`), each around one command or one file operation
and each with a deadline. The holder's waiting lock on its own new record (section 3) is one more:
the only other process that can hold that lock is a reader probing liveness, which holds it across a
stat and at most an unlink, takes nothing else meanwhile, and loses it if it dies. The wait is still
bounded at 1 s, after which the set runs without a record and its refusals fall back to the generic
message.

### Why not an existing lock

Two locks already exist and neither fits. `db/backup.py:477-543` gives shared and exclusive locks
through SQLite transactions on side files; it would put sub-VM entities on a different mechanism
from the vm level, which must stay `flock` and `LockFile` on today's byte to interlock with older
releases, and it costs a SQLite file and connection per entity for a one-byte question. The guest
file program's lock (`execution/file_program.py:41`) lives on the guest and waits up to 30 s; it
makes one file operation consistent among identities on one VM, not one workstation operation
exclusive against another, and it cannot fail fast or name a holder.

### Prior art: multi-granularity locking

The tree is Gray's multi-granularity locking (Gray, Lorie, Putzolu and Traiger, "Granularity of
Locks and Degrees of Consistency in a Shared Data Base", 1976). There, an operation locks a node S
or X after holding an intention mode (IS or IX) on every ancestor, and on a DAG it takes the
intention on every parent, which is this design's rule for a session's two parents. Intention modes
are compatible with each other and conflict with X, which is exactly what shared on an ancestor does
here, so shared plays the intention role. Gray also has a real S on an inner node, which locks a
whole subtree for reading and conflicts with IX below it. This design reads a whole subtree in one
place, the recursive copy of a rehome or copy source, and takes that tree's path exclusive (Gray's
X) rather than adding S: the only cost is that two copies of one source run one after the other, and
it keeps the design to two modes. Workspace entities need no S: copy's source workspace is held
shared so it cannot be deleted or rehomed, while sessions in it keep starting and stopping by
design. If two concurrent readers of one tree ever mattered, the design would add Gray's S beside
separate intention modes.

## 2. The operation sequence

Every command that changes native state runs one sequence. It keeps ADR 0019's gate-first shape
(`docs/adrs/0019-orchestration-layer-command-plans-over-node-graphs.md`, "The activation gate": the
gate opens before the preflight sweep, and its secrets resolve just in time), honors R4 (nothing
below the VM holds the vm exclusive beyond a boot) and R6 (no prompt under a lock but the three
sanctioned acquisitions below):

1. **Read**: the database reads that build the whole request. Target choosers that pick the entity
   belong here (session create's plan prompts, `sessions/manager/_create_plan.py:175`, `:349`,
   `:404`; copy's destination chooser, `workspaces/manager/_common.py:117`).
2. **Probe** the request (`probe_locks`), without its `path` entities, which join at step 6: a
   conflict fails here, naming its holder, before any prompt or boot.
3. **Confirm early**: the confirmations that today precede the gate (`workspace delete`,
   `workspaces/manager/delete.py:101`; `agent delete`, `agents/manager/lifecycle.py:324`;
   `vm delete`, `vms/manager/power.py:213`).
4. **Gate**, when the operation needs the VM running (below). If `vm_node.confirmed_active()`
   (`vms/nodes.py:196-208`) holds, the gate resolves nothing and boots nothing (ADR 0019's fast
   path). Otherwise it resolves the gate secrets just in time and asks the provider; only when the
   provider reports the VM stopped or deallocated (`observed_stopped`, `nodes.py:210-223`), never on
   a Tailscale miss alone, it takes `{vm: exclusive}`, runs `auto_start` under it
   (`nodes.py:236-271`, which keeps its re-read of the row, intent and provider status under that
   exclusive and starts only a stopped VM), and releases it. The gate then admits the `vm_active`
   hold (a keepalive, not a lock) under a brief `{vm: shared}`: it re-reads the row, skips the hold
   under stopped intent, enters it and releases the lock, exactly as `hold_active` does today
   (`nodes.py:273-285`). The hold lasts to the end of the command.
5. **Prepare**: the preflight sweep, read-only guest probes, the boundary resolve, and the
   confirmations that render probe results. Nothing is locked.
6. **Acquire** the request once, with the vm shared.
7. **Re-check** under the set the fields of [What step 7 compares](#what-step-7-compares), against
   what step 4 admitted for the VM row (or what step 1 read, for a command that skips step 4) and
   what steps 1 and 5 read for everything else. A `vm stop`, `delete`, `rekey` or `recover` that ran
   in between shows here. Any difference refuses with `StateConflictError` ("changed since this
   command read it", hint "Run the command again.").
8. **Run** the body; nested operations call `held.require`.

**Needs the VM running** means every command that opens the activation gate today
(`gated_vm_boundary`, `vms/manager/boundary.py:192`; `activation_gate`,
`orchestration/activation.py:158`; the runtime path's `activate_vm_runtime`), except stop paths,
which never boot and skip the hold (`orchestration/README.md:193-194`). The lock-set table marks it
per command. A command that does not need it skips step 4.

`gated_vm_boundary` and `activation_gate` become this sequence. The probe (step 2) is a separate
call the command makes after its step-1 reads and before any step-3 confirmation. The boundary then
takes the request, runs steps 4 and 5 on entry, and yields an object whose `locked()` runs steps 6
and 7 and yields the held set, so a command renders its probe-backed confirmations inside the
boundary before locking.

**Access and observe commands** (`vm shell`, `vm exec`, `vm port-forward`, `vm backup`,
`agent shell`, `agent exec`, `session attach`, `session logs`, `console attach`) run steps 1, 2, 4
and 5 with the admission request. Step 4's brief shared admission is their whole lock: they acquire
nothing more and run their body unlocked under the `vm_active` hold, as today
(`cli/command-reference.md:690-691`). One edge of R4 stays as it is today: an access command that
must cold-boot the VM needs the vm exclusive for the boot, so an operation holding anything below
the VM refuses that boot; once the VM is up, nothing below the VM refuses access.

**VM-wide commands** run the same sequence and take `{vm: exclusive}` at step 6. `vm reinit` gates
at step 4 like any other command; `vm rekey` resolves its boundary before its gate, which serves the
boundary's cache (`vms/manager/power.py:457`, as ADR 0019 describes), so its step 5 precedes step 4.
Under the held exclusive, `auto_start` may run again if the VM went down meanwhile, since the set
covers it. `vm start` and `vm recover` power the VM as their operation and never gate, as ADR 0019
says of commands whose op is the power change.

### Runtime families

The batch commands (`runtime_state` for `session start`, `stop`, `restart`, `reconcile`,
`vm reconcile` and `console reconcile`) and the console runtime commands (`console start`,
`restart`, `stop`) keep the runtime path's order at HEAD, which differs from the gate path's
(lettered here so as not to read as the eight steps above). Steps (b), (d) and (e) run per family in
VM name order, and every surviving family finishes (e) before any family takes its final set at (f):
that barrier keeps every infrastructure prompt outside the final sets, and lets the one workload
boundary at (h) hold all of them.

- **(a) Read** the selection (`runtime_state/_selection.py:28-76`).
- **(b) Probe** each family's set; a refusal drops targets as the per-target rule below says.
- **(c) Confirm early**: restart consent, after its status observation, as today
  (`sessions/manager/_lifecycle.py:404-411`). Console start and restart run their known-policy
  preflight here (`preflight_console_vm`, `multi_console/lifecycle.py:78`; stop's at `:154`), before
  any intent is written, so a known refusal keeps the old target
  (`tests/sessions/test_console_preparation.py:345`).
- **(d) Record intent** under a brief acquisition of the family's own set (vm shared, targets
  exclusive), released at once. The existing acceptance runs under it: the guest status probes it
  reads (`_prepare.py:68-90`, unlocked just before, a failure counting as uncertainty), the
  unauthorized-running-restart refusal, and the stopped-parent refusal (`_prepare.py:113`,
  `:130-131`). Console runtime commands record their intent (`multi_console/lifecycle.py:86`) the
  same way, after their parent and member checks under that brief set: start and restart re-read the
  selected console and its VM (`guarded_console_selection`, `multi_console/_helpers.py:221-240`),
  and stop keeps its current parent check before it records stopped. The accepted declaration is
  noted: each target's desired overlay digest (`db.instance_state.get_desired_overlay`) as (d) left
  it.
- **(e) Infrastructure**, unlocked, in the runtime path's order: the infrastructure preflight per
  node, a second pass distinct from (c)'s known-policy preflight, whose failure keeps the intent (d)
  recorded (`vms/manager/runtime.py:68-69`); gate secrets just in time through
  `invocation.gate_value` and `resolver.resolve_gate` (`runtime_state/_models.py:100-104`); the
  provider observation (`runtime.py:74-79`), with no `confirmed_active` fast path on this path; and
  the boot, under a brief `{vm: exclusive}` when the family needs its VM running and the provider
  reports it stopped (`_prepare.py:247`).
- **(f) Acquire** the family's set, with the vm shared.
- **(g) Re-check** the existing selection checks (`_selection.py:103-149`), the VM row, that the
  recorded intent still stands, and that each target's desired overlay digest is still the one (d)
  accepted. A restart prepares its launch definition from that declaration
  (`runtime_state/_prepare.py:118-128`) and executes it later (`sessions/manager/_execute.py:72`);
  without this check, two restarts accepting overlays X then Y could leave the later-finishing one
  launching X while Y is stored. A changed digest refuses that target.
- **(h) Run**: guest workload preparation, then the workload boundary
  (`runtime_state/_execute.py:45`; the console commands' pane resolve,
  `multi_console/lifecycle.py:117-118` and `:191`), then execution. The boundary comes last because
  the resolver forbids a gate secret after it (`secrets/resolver.py:200-201`), and it resolves while
  every selected family's set is held, each set showing its own notice.

Consequences:

- **Intent survives infrastructure failure.** Intent is recorded before the gate secrets, the
  observation and the boot, so a failure in any of them leaves it recorded, which keeps
  `test_start_acceptance_survives_infrastructure_failure`
  (`tests/sessions/test_console_preparation.py:306-322`) and the stopped-parent contract
  (`:325-341`).
- **Refusal after recorded intent is acceptable, and says so.** A family whose acquisition at (f) is
  refused keeps the intent it recorded at (d). That is the same contract as a failed activation
  today: intent is the desired state, and the next start or reconcile converges it. The refusal
  tells the operator: each blocked target's `RuntimeResult` gains `restore_intent`, the command
  words that put back the intent it replaced (for example
  `("agw", "session", "set-desired-state", <name>, <previous>)`, or the console's), and the rendered
  result says the intent was recorded and shows that command. When the replaced value has no public
  form (a legacy NULL desired state, which `set-desired-state` cannot write since it accepts only
  running and stopped), `restore_intent` is empty and the result says only that the new intent stays
  accepted.
- **Per-target and per-family failure.** One rule covers every acquisition of the sequence: when an
  acquisition at (b), (d) or (f) is refused at an entity other than the vm, the family drops every
  target whose request needs that entity in the refused mode (so two targets that need one refused
  workspace exclusive both drop) and retries with the rest. A target that needs the entity only in a
  weaker mode survives, and the retry asks the weaker union; if that is refused too, its targets
  drop in turn. Each attempt is all-or-nothing and only ever smaller. So a reconcile selecting
  session M and a stopped-intent console C that contains M, while another operation on console D
  holds M shared, drops M (the exclusive contributor) and reconciles C, which needs M only shared. A
  refused vm, or a failed provider observation, gate secret or boot, fails the family, as
  `_selection.py:122-128` does for a busy family today. A target whose declaration changed at (g)
  fails alone. So today's selection and bulk-stop paths keep a successful sibling on the same VM.
  Each result is recorded on its target; none is raised through `run_selected` to the whole
  invocation.
- **No boot under shared.** A power action found necessary under a family's shared vm (the VM went
  down after (e)) refuses with
  `StateError("VM '<name>' stopped after this command checked it", hint="Run the command again.")`;
  activation calls `held.require({vm: exclusive})` first.
- **`vm reconcile`.** Its family requests what its children need: each selected session and console
  exclusive, the vm shared. It takes the vm exclusive in place of shared only when the VM's own
  target is stopped, since that family then stops the VM.

**Agent reinit** keeps its early declaration acceptance: it saves the template binding and the agent
overlay before building its graph and capturing sources (`agents/manager/lifecycle.py:570-579`, "The
binding remains if setup fails"). It does that write under a brief `{account: exclusive}` (vm
shared) after its probe, releases, runs the gate and preparation from the declaration it saved, and
at step 7 compares the agent's template and desired overlay digest with what it saved, so the guest
setup it runs is bound to the declaration it prepared. `vm reinit` accepts its declarations later,
under its final vm exclusive (`vms/manager/reinit.py:302-306`), and is unchanged.

`session start --spec` (`sessions/manager/_spec.py:103`) acquires nothing itself. It runs its
consent probe and confirmation unlocked (step 5; today under its guard, `:133-135`), then passes the
overlay and `running_restart_authorized` into the batch, which persists the overlay at its own (d)
alongside the intent and runs the normal family sequence. `run_session_batch(held=)` (`:149`) goes
away.

### Prompts under a set

Three acquisitions are sanctioned to prompt, because the questions are about what the locked
operation is doing: `vm recover`, for its host-key confirmation (`vms/manager/recover.py:240`);
`vm rekey`, for `--wait-for-share` (`vms/manager/power.py:490`); and the step-6 set of a
`runtime_state` family or console runtime command, for workload and pane secrets, which depend on
definitions and observations made under it (`runtime_state/_execute.py:45`,
`sessions/multi_console/lifecycle.py:117-118`, `:191`). The first two hold only the vm, exclusive
anyway; the third holds only its family's or console's set (for `vm reconcile`, its children with
the vm shared, or the vm exclusive when the VM's target is stopped). These call sites acquire with
`native_locks(db, request, prompts=True)`. Every other console command that resolves pane secrets
(`console create`, `add-sessions`, `add-shell`, `restore-session`: `multi_console/attach.py:495`,
`crud.py:379`, `:696`, `restore.py:353`) resolves them at step 5 and re-checks at step 7 what it
resolved them for: its console row, the sessions it adds or restores, and for `console create` and
`restore-session` the members they rebuild from.

The notice is structural. `native_locks` keeps the sets the current invocation holds, in a
`ContextVar` (so two invocations sharing one process, such as concurrent tests or threads, never see
each other's sets), and the prompt helpers (`output.confirm`, `choose`, `pause`, `prompt`,
`prompt_secret`, `output.py:618-640`, which the resolver's terminal entry uses) call
`native_locks.prompt_notice()` before they prompt. For each held set marked `prompts=True` that has
not yet shown it, it prints one `output.info` line naming the set's entities and saying they stay
locked until the answer is given, then marks the set, so each notice appears once, before that set's
first prompt. Under any held set not marked `prompts=True` it raises `StateError`: a prompt there is
a programming error, which the test suite catches.

## 3. Holder records

### Format and location

One JSON file per held set, `<sha256(vm)>.d/holders/<16 hex>.json`, mode 0600, its name random:

```json
{
  "pid": 4242,
  "command": ["agw", "workspace", "rehome"],
  "acquired": 1795400000.25,
  "entities": [
    { "key": "vm/dev", "mode": "shared" },
    { "key": "workspace/api", "mode": "exclusive" }
  ]
}
```

`command` is `native_locks.operation_label`, a `ContextVar[tuple[str, ...]]` defaulting to
`("agw",)`. The CLI layer sets it, never the service layer: one group class shared by the Typer apps
(`cli/_app.py:17` and the apps in `cli/commands/`) overrides Click's `resolve_command` to append
each resolved command name, so the label is the command path and contains no argument or option
values. `acquired` is wall-clock acquisition time; `pid` is for diagnosis only.

### Write and remove

- After acquisition succeeds, the holder creates the file with `O_CREAT | O_EXCL`, takes an
  exclusive lock on it (POSIX `flock`; Windows `LockFileEx` exclusive at offset `1 << 62`, past the
  content, so readers can still read it), and writes the JSON. It takes that lock waiting, since a
  reader's liveness probe holds the record for an instant, bounded at 1 s (past that, the set runs
  without a record). On POSIX it then checks the file is still linked (`os.fstat(fd).st_nlink`): a
  reader that probed between the create and the lock found it free and removed it, so the holder
  creates a fresh one. On Windows a reader cannot remove a file the holder has open. The handle and
  its lock live as long as the set.
- On release the holder retires the record first: it unlocks, closes and unlinks it, and only then
  releases its first entity. A refusal during the handoff finds no record and prints the generic
  message, never a former holder. On Windows the unlink can fail while a reader has the file open;
  the failure is ignored, and the next reader finds the record unlocked and removes it.

### Stale records

A record is live exactly while its own lock is held, which the OS releases when the holder dies. A
reader tests liveness by trying a shared lock on the record without waiting (same offset on
Windows). A record that is locked but does not parse (a reader raced the holder's write) is skipped.
If the reader gets the lock, the holder is gone, or has created the file and not yet locked it:

- On POSIX the reader unlinks the record while still holding its shared lock, then unlocks and
  closes. A holder waiting to lock that file gets the lock only after the unlink, so its link check
  always sees the removal and recreates the record; there is no gap in which a live holder's record
  can vanish unnoticed.
- On Windows the reader unlocks and closes, then deletes. A live holder still has the file open
  (Python opens without `FILE_SHARE_DELETE`), so the delete fails and is ignored; only a dead
  holder's record can be removed.

No pid is probed, so a reused pid cannot make a dead holder look alive.

### Naming the holder

On a refusal at entity `e` in mode `m`, the refused process lists the VM's `holders/`, drops stale
and malformed records, keeps those listing `e` in a mode that conflicts with `m`, and names the one
acquired earliest. The record is advisory: the OS lock already decided, so a missing record only
falls back to the generic message.

The R3 error and hint, with the subject the first exclusive entity of the refused request (or
`vm/<name>` for an admission request):

```text
Error: session/build-42 on VM 'dev' is blocked: `agw workspace rehome` has held workspace/api for 3m12s
  Hint: Retry when that operation finishes.
Error: session/build-42 on VM 'dev' is blocked: another operation holds workspace/api
  Hint: Retry when that operation finishes.
```

Durations print as `41s`, `3m12s` or `1h05m`. "Is blocked" stands in for the refused verb, which the
primitive does not know; the subject and the holder already tell the operator what to wait for.

### What a record never contains

Argument or option values, secret names or values, environment, hostnames, prompt answers, or
anything else read from the operator, beyond its entity keys. The keys are entity names already in
the database beside it, and the resolved paths of `path` entities, which a refusal needs to name the
tree it is waiting for. Those paths are allowed because of where records live: the lock directory is
created 0700 (`harness_setup/locking.py:53`) beside the operator's database in their own config
directory (`db/__init__.py:88`), so only that OS user can read a record, and that user chose the
paths. The only fields are the ones above.

## 4. Lock sets per command

### Rules

- **Secrets and confirmations come before acquiring** (steps 3 to 5), except in the three sanctioned
  acquisitions of [Prompts under a set](#prompts-under-a-set). A confirmation that shows guest state
  runs its read-only probes at step 5 and again under the set at step 7.
- **Confirmations bind the named slot.** An unlocked confirmation names a workspace, agent, session
  or console by its name (and a workspace by its path); step 7 compares the fields of the next
  section, so a replacement under the same name and path with the same group passes. That is the
  intended binding: the operator confirmed the named slot, and no new identity field is introduced
  for workspaces or agents, which have none today.
- **Read, lock, re-check.** Each acquisition takes its whole request, built from a step-1 read (a
  runtime family's retry may drop targets, never add one): the dependents an operation will change
  (an agent's sessions, grants and consoles; a workspace's; the grant-all agents a workspace create
  will grant) are all in it, and all of it is read before any native change. After acquiring, the
  operation re-reads; if the rows, memberships or sets it read changed, or no longer match what a
  confirmation showed (the session count of an agent or workspace delete), it refuses with
  `StateConflictError` before any native change.
- **Lists are rewritten from what the final set shows.** A command that changes a console's lists
  (its members, their order, a member's shells) resolves only the secrets of what it adds at step 5,
  then reads the lists, derives positions and indexes, and writes them at step 8 under the console
  exclusive. `console add-shell` (`sessions/multi_console/crud.py:648-704`) therefore appends to the
  shell list it reads under the final lock, not to one read before it, and two concurrent appends
  cannot lose one. `console restore-session` likewise rebuilds its pane plan (`restore.py:315-353`)
  from the console, its members and their shells read under the final set, and refuses if that plan
  needs a secret step 5 did not resolve.
- **Nested operations require.** `realize_workspace`, `realize_agent`, harness setup
  (`harness_setup/dispatch.py:116`, `harness_setup/lifecycle.py:262`, `:312`, `:365`), teardown
  deletes and the empty-console delete call `held.require` with their own request, which their
  caller's request covers.
- Dry runs take no lock, as today.

### What step 7 compares

A command compares only what it read, and only these fields, never whole rows (timestamps and
recorded pids change under ordinary use):

| Read at step 1 or 5               | Fields compared                                                                                                                                                                                                                                               |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| VM row                            | `vm_uuid`, `desired_runtime_state`, `tailscale_host`; `confirm-release` adds `debian_release`; `vm reinit` adds `template`, `admin_template` and the stored overlay it read                                                                                   |
| workspace row                     | presence, `vm_name`, `workspace_path`, `linux_group`                                                                                                                                                                                                          |
| agent row                         | presence, `vm_name`, `linux_user`, `grant_all`; agent reinit adds `template` and the desired overlay digest it saved                                                                                                                                          |
| session row                       | `session_uuid`, `workspace_name`, `agent_name`, `mode`; runtime families add `desired_runtime_state` against the intent they recorded and the desired overlay digest (`db.instance_state.get_desired_overlay`) they accepted                                  |
| console row and membership        | presence, `vm_name`, `admin_shell`, the ordered member session names; a command that rewrites the list from the read under the set (`add-sessions`, `add-shell`) compares only the console row, and compares each session it adds by its session row's fields |
| the sets a request was built from | the session names of a deleted agent or workspace (and the count its confirmation showed), its grant pairs and the consoles holding its sessions; the grant-all agent names a workspace create grants; the workspaces a grant-all writer grants or revokes    |
| path entities                     | the lookup walk's recorded directories and final path for each stored path, walked again on the guest                                                                                                                                                         |
| guest probes                      | the outcome of each probe the command re-runs: rehome's not-stopped sessions, source directory, unoccupied target and overlap; session delete's status and access checks; a runtime family's target status                                                    |

### Lock sets

X is taken exclusive; every ancestor of an X entity is taken shared and not listed; S lists further
shared entities. A console's ancestors are the vm and its member sessions with their workspaces and
accounts. Gate says whether the command needs the VM running and so runs step 4.

| Command                                                                                           | X                                                                                                                                                                                                                                         | S                                                   | Gate                                          |
| ------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- | --------------------------------------------- |
| `vm create` (by name), `start`, `stop`, `delete`, `recover`, `set-desired-state`                  | vm                                                                                                                                                                                                                                        |                                                     | no                                            |
| `vm reinit`, `rekey`                                                                              | vm                                                                                                                                                                                                                                        |                                                     | yes                                           |
| `vm import-host-keys`                                                                             | vm, per VM                                                                                                                                                                                                                                |                                                     | no                                            |
| `vm confirm-release`                                                                              | vm                                                                                                                                                                                                                                        |                                                     | yes                                           |
| `vm reconcile`                                                                                    | each session and console on the VM, per family; the vm when its own target is stopped                                                                                                                                                     |                                                     | per family, from intent                       |
| access and observe commands                                                                       | none: admission only                                                                                                                                                                                                                      | vm                                                  | yes                                           |
| `agent create`                                                                                    | account, path(predicted native-home root) for a root outside HOME ([Harness setup confinement](#harness-setup-confinement)); with `--grant-all-workspaces` also grant-all and a grant per workspace on the VM                             |                                                     | yes                                           |
| `agent reinit`                                                                                    | account, path(predicted native-home root) for a root outside HOME, a grant per recorded grant it replays (`agents/manager/lifecycle.py:783`)                                                                                              |                                                     | yes                                           |
| `agent delete`                                                                                    | account, each of its sessions, grants and consoles holding them; grant-all when the agent is grant-all                                                                                                                                    |                                                     | yes                                           |
| `agent grant-workspaces`, `revoke-workspaces`                                                     | a grant per named workspace; `--all`: grant-all and a grant for every workspace on the VM, for both commands                                                                                                                              | account (always, even with no workspace)            | yes                                           |
| `workspace create`                                                                                | workspace, path(target), grant(workspace, admin), grant(workspace, agent) per grant-all agent                                                                                                                                             | grant-all                                           | yes                                           |
| `workspace delete`                                                                                | workspace, path(its stored path, when it removes the files), each of its sessions, grants and consoles holding them                                                                                                                       |                                                     | yes                                           |
| `workspace repair`                                                                                | workspace, grant(workspace, admin), a grant per agent holding one on it                                                                                                                                                                   |                                                     | yes                                           |
| `workspace rehome`                                                                                | workspace, path(target), path(source)                                                                                                                                                                                                     |                                                     | yes                                           |
| `workspace copy`                                                                                  | destination VM: workspace, path(target), grant(workspace, admin), grant per grant-all agent; source VM: path(source)                                                                                                                      | destination: grant-all; source VM: source workspace | yes, both VMs                                 |
| `session create`                                                                                  | session; grant(workspace, agent) in agent mode; `--new-workspace` adds workspace, path(target), grant(workspace, admin) and the grant-all grants; `--new-agent` adds account and path(predicted native-home root) for a root outside HOME | `--new-workspace`: grant-all                        | yes                                           |
| `session start`, `restart`, `reconcile`                                                           | each selected session, per family                                                                                                                                                                                                         |                                                     | yes (reconcile: unless its targets only stop) |
| `session stop`                                                                                    | each selected session, per family                                                                                                                                                                                                         |                                                     | no                                            |
| `session set-desired-state`                                                                       | session                                                                                                                                                                                                                                   |                                                     | no                                            |
| `session delete`                                                                                  | session; grant(workspace, agent) in agent mode; each console holding it                                                                                                                                                                   |                                                     | yes                                           |
| `console create`, `start`, `restart`, `restore-session`                                           | console                                                                                                                                                                                                                                   |                                                     | yes                                           |
| `console reconcile`                                                                               | console                                                                                                                                                                                                                                   |                                                     | unless it only stops                          |
| `console stop`, `set-desired-state`, `delete`, `remove-sessions`, `reorder-sessions`, `add-shell` | console                                                                                                                                                                                                                                   |                                                     | no                                            |
| `console add-sessions`                                                                            | console                                                                                                                                                                                                                                   | each added session                                  | no                                            |
| `vm describe`, `vm verify-connection`, `vm logs`, every `describe` and `list`                     | none                                                                                                                                                                                                                                      |                                                     | no                                            |

### Rows that need more than a line

**Grant-all.** Workspace create (and copy's destination, and `session create --new-workspace`) reads
the grant-all agents at step 1, requests a grant for each, and re-reads them under its shared
`grant-all`; agent create with grant-all and grant or revoke `--all` read the workspaces at step 1
and re-read them under their exclusive `grant-all`, which no concurrent workspace create can hold.
Both sides therefore finish their reads before any native change.

**Bulk grant and revoke.** Both `--all` forms lock a grant for every workspace on the VM, not only
the pairs already granted, and hold the account shared even when the VM has no workspace. Revoke's
database delete is account-wide (`db/database.py:773`) while its native cleanup visits only the
workspaces it snapshotted (`agents/grants.py:176`); locking only the granted pairs would let a named
grant of another workspace add its membership and row in between, and the delete would then remove
the row and leave the membership. With every pair locked, that grant is refused.

**Workspace copy across two VMs.** One set per VM, probed and acquired in VM name order, one set
when both are the same VM. The source workspace is held shared so it cannot be deleted, rehomed or
repaired mid-copy; sessions in it keep running. Copy's destination chooser (`_resolve_vm`,
`workspaces/manager/_common.py:117`, reached from `copy.py:140`) is step 1.

**Session delete.** In agent mode it may remove the implicit grant and the group membership
(`sessions/manager/_queries.py:253-262`), so it holds that grant exclusive, which refuses a
concurrent `session create` for the same pair and a `revoke-workspaces` of it. When that grant is
the agent's last on the workspace, the membership is removed before the session and grant rows are
deleted ([Account files](#account-files)). Removing a console member changes the console, so it
holds each console holding the session exclusive, with the console's other members shared. Its
confirmation (`_queries.py:227`) is step 5, after its status and access probes. A status that needs
a PID repair cannot be read before the set; it is left undecided there, and the repair and the
decision run under the set. The empty-console, workspace and agent follow-on offers run after the
set is released, all in one shape: the delete each triggers runs its own sequence, whose re-check
refuses if the console, workspace or agent is no longer empty. `console remove-sessions` offers the
same way.

**Workspace repair.** Besides restoring the admin's and each granted agent's membership, repair
removes guest members of the workspace group who hold no grant (`workspaces/manager/repair.py:329`).
Those have no grant entity to lock, and none is needed: the workspace exclusive refuses every grant
or revoke on that workspace, since each holds the workspace shared, so nothing can be adding them
meanwhile.

**`vm import-host-keys`.** Today it takes no lock. Each VM's `_import_one`
(`vms/manager/host_keys.py:126-149`) runs under that VM's `{vm: exclusive}`; a refusal is a new
per-VM `BUSY` outcome carrying the R3 message, the other VMs carry on, and the final SSH config sync
still runs.

**`vm confirm-release`.** Today its database writes take no lock and no re-check
(`vms/manager/release.py:86-89`). Its confirmation (`:79`) is step 5; it then takes
`{vm: exclusive}`, re-reads the row's uuid and the observed release, and writes.

**Session create.** Plan prompts and graph build are step 1; `require_vm_ssh_boundary` and the gate
(`sessions/manager/_create.py:413-414`) are step 4; `_preflight_and_resolve` (`:417`) is step 5; one
acquisition covers the session, grants and any new workspace or account; the existing VM reload
(`_create.py:378-386`) joins step 7. The held set is attached to the graph so `realize_workspace`,
`realize_agent` and the session slice `require` it. Rollback runs inside the set:
`PendingAgentNode.teardown` and `PendingWorkspaceNode.teardown` call `delete_agent` and
`delete_workspace` (`agents/nodes.py:219`, `workspaces/nodes.py:151`), whose requests name only this
create's own account or workspace, session and grants, all already in the set, so a rollback is
never refused.

**Workspace rehome.** Step 5 is pure observation: its not-stopped-sessions check observes statuses
without persisting anything (today it runs `ensure_pids_batch`, which can write a PID repair, under
the guard and before any gate, `workspaces/manager/rehome.py:101-123`), then its directory probes
(`:191-206`) and its confirmation (`:217`), which shows only the stored and new paths. Under the
workspace exclusive, step 7 re-reads the stored path, resolves the source and target again, runs
`ensure_pids_batch` and the sessions check (a session can start between the two) and the probes
again; the target is then reserved with the exclusive `mkdir` it uses today (`:228`). Workspace
repair keeps its writes under its own lock.

**Workspace paths.** Every operation that creates, fills, removes or recursively reads a workspace
tree locks it as a `path` entity, exclusive: workspace create, copy's destination and
`session create --new-workspace` take the target; rehome takes the target and its source, with or
without `--remove-old`; copy takes its source; workspace delete takes its stored path when it
removes the files. A recursive reader takes its source exclusive because a shared source would let a
managed move inside it run during the read: a rehome of `/tree` without `--remove-old` copying while
a child rehome moves `/tree/before` to `/tree/after` with `--remove-old` would copy both names,
where either serial order copies one. Disjoint trees still proceed together. Each operation also
takes, shared, every directory and followed link its lookup walk reports ([Entities](#entities)).

**The lookup walk.** One read-only guest helper, `native_locks.path_walk`, a plain-stdlib program
run on the path's own VM like the guest file program, serves create, copy (once per VM: the source
walk on the source VM, the destination walk on the destination VM), rehome and delete. It takes the
stored paths as `stored_workspace_path` returns them (`workspaces/guest.py:51`), several in one
call, and resolves each component by component as the kernel does: starting at `/`, it looks each
name up in the current physical directory and records that directory; a `.` stays, a `..` records
the current directory, where `..` is looked up, then moves to its parent; a symbolic link has its
own entry (the current directory joined with its name) recorded as a followed link, is read with
`readlink`, and its target's components are pushed in front of the rest, and an absolute target
restarts from `/`; a missing name ends the physical part, and the remaining names are joined
lexically, so a target not yet created or a directory already gone still resolves. Past 40 link
expansions (the kernel's limit) it refuses with `StateError`, as the kernel would with `ELOOP`. For
each path it emits, NUL-separated, every directory and followed link it recorded, then the final
path. The walk runs at step 5, its names join the request at step 6, and it runs again at step 7;
any difference in either list refuses with `StateConflictError`.

The walk only names the locks; each body keeps operating on the spellings it uses today, with one
exception. Workspace delete removes the path as `stored_workspace_path` normalizes it rather than
the row's raw spelling, which it passes to `rm -rf` today (`workspaces/manager/delete.py:256`,
`workspaces/backends/vm.py:287`): a stored trailing slash would make `rm` follow a final link and
empty its target, the very removal the next sentences rule out. An operation that uses the referent
(create, copy, rehome) has the walk follow the final component. Workspace delete only removes the
terminal entry, so a stored path whose final component is a symbolic link is cleaned up by removing
the link, never its target; for it the walk resolves every component but the last, names the entry
itself (the resolved parent joined with the last name) without reading it, and delete locks that
entry exclusive and the recorded directories shared. A dangling or cyclic final link therefore names
one entry like any other and never refuses the delete. A final `..` names no entry, so it is walked
in full. The cases today's delete tolerates keep their behavior: a path already missing walks to its
lexical remainder, is locked, and its removal is the no-op it is today; a stored path that names no
one directory (`stored_workspace_path` refusing it, `workspaces/manager/delete.py:66-73`) is not
touched, so it takes no path lock.

- The earlier tester case: A's rehome to `/data/B/new` holds `/data/B` shared, and B's rehome needs
  `/data/B` exclusive, so whichever acquires second is refused. Two creates of disjoint paths
  proceed together.
- A followed link: workspace A stored at `/front/link`, a link to `/physical`, and B at
  `/front/link/child`. A's delete takes the entry `/front/link` exclusive; B's rehome follows that
  link, so it takes `/front/link` shared beside `/front` and `/physical`. Whichever acquires second
  is refused, so B can no longer copy out of `/physical/child` while A removes the link its
  old-spelling cleanup goes through.
- Links in the middle: A's stored source `/front/link/a`, with `/front/link -> /middle/bridge` and
  `/middle/bridge -> /physical`, walks through `/front`, `/middle` and `/physical` to `/physical/a`,
  following the links `/front/link` and `/middle/bridge`. A holds those three directories and two
  links shared and `/physical/a` exclusive, so a rehome of `/front` or `/middle`, or a delete of
  either link, is refused while A runs. Two links (`/c/l1 -> /e`, `/e/d/l2 -> /f`, stored
  `/c/l1/d/l2/a`) record `/c`, `/e`, `/e/d` and `/f` and the followed links `/c/l1` and `/e/d/l2`; a
  `..` after a link (`/data/x -> /p/q`, stored `/data/x/../y`) records `/data`, `/p` and `/p/q` and
  the followed link `/data/x`.
- No overlapping-workspace reads are needed. Any operation that could destroy or read another
  workspace's tree holds that tree's root or an ancestor exclusive, and any operation working inside
  it holds the root shared, so the path locks already refuse every concurrent overlap; an idle
  workspace whose tree is not being changed constrains nothing, exactly as today.
- Nothing about what is allowed changes. A target inside or containing another workspace's tree is
  still accepted, as today, and the exact-target `mkdir` (`:228`) still reserves it.

A nested legacy layout can still lose a workspace sequentially: a rehome of A with `--remove-old`
removes any workspace whose tree lies inside A's source, today and under this design alike, since
the two operations do not overlap in time. That is not a locking question, and is tracked as #957.

## 5. Shared state across entities

| State                                    | Mechanism                                                                                                                                                                                                                                                                                                                                                           |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| VM account and group files               | shadow-utils' own serialization, below; `ensure_group` made idempotent                                                                                                                                                                                                                                                                                              |
| an agent's home and mail spool           | today's `userdel -r` under the account's exclusive, after a check that the account's processes stopped; a failed removal keeps the agent's row; a concurrent account edit on the VM waits on shadow's lock and past its 15 s fails as a retryable `CommandFailed` ([Agent account removal](#agent-account-removal))                                                 |
| `/opt/agentworks/tmux-session.conf`      | already an atomic replace: `deploy_restricted_config` (`sessions/tmux.py:711-722`) writes through `Files.write`, an `O_EXCL` temporary with a fresh name renamed over the file (`execution/file_program.py:298-328`), whose parents tolerate a concurrent `mkdir` (`:126-129`)                                                                                      |
| socket roots and per-account socket dirs | already convergent: each step of `ensure_agent_socket_root`, `ensure_agent_socket_dir` and `ensure_admin_socket_root` (`tmux.py:456-511`, `:607-632`) is `mkdir -p` or an absolute `chown`/`chmod`, so any interleaving ends in the same state                                                                                                                      |
| stale socket cleanup                     | `cleanup_stale_sockets` (`tmux.py:546`) runs only from agent create and reinit (`agents/initializer.py:227`, under the account exclusive) and vm create and reinit (`vms/initializer/driver.py:349`, under the vm exclusive); a launch holds its account shared                                                                                                     |
| workspace group membership               | the grant entity                                                                                                                                                                                                                                                                                                                                                    |
| the grant-all agents and the workspaces  | the grant-all entity                                                                                                                                                                                                                                                                                                                                                |
| workspace directory trees                | `path` entities: the walk's final path exclusive and every directory it looked a name up in shared, for whoever creates, fills, removes or recursively reads the tree ([Workspace paths, section 4](#rows-that-need-more-than-a-line)); the exact-target `mkdir` in create (`workspaces/backends/vm.py:127`) and rehome (`rehome.py:228`) still reserves the target |
| console membership                       | the console entity, with its members as parents                                                                                                                                                                                                                                                                                                                     |
| the dpkg lock                            | workspace repair's `acl` install (`workspaces/manager/repair.py:158`) gains `-o DPkg::Lock::Timeout=120` inside a 180 s install deadline, as `vms/recovery.py:117-118` uses, since two repairs of different workspaces can now run at once                                                                                                                          |
| operation logs                           | each log gets a unique name created with an exclusive open ([Operation logs](#operation-logs))                                                                                                                                                                                                                                                                      |
| operator SSH config                      | the SSH config rebuild mutex ([below](#ssh-config-rebuild))                                                                                                                                                                                                                                                                                                         |
| host-key store                           | per-VM file written atomically (`execution/trust.py:242-261`) under the vm exclusive                                                                                                                                                                                                                                                                                |

VS Code workspace files are written and removed per workspace inside workspace create and delete
(`workspaces/backends/vm.py:350`, `workspaces/manager/delete.py:263`), under that workspace's
exclusive, and by `vm delete` (`vms/manager/power.py:308`) under the vm exclusive. Their bulk
regeneration (`cli/commands/config.py:80`) already races today and is tracked as #954.

### Account files

No agentworks lock wraps the account and group edits. Both supported guests (`debian.py:26-27`:
bookworm and trixie) ship shadow-utils that serializes its own tools: in the shadow sources for 4.13
(bookworm) and 4.17.4 (trixie), `commonio_lock` calls glibc's `lckpwdf`, which waits up to 15 s (and
on a bookworm host `useradd` links `lckpwdf`). An integration run confirms it on a trixie guest
([section 6](#6-tests)). The one check-then-act that can now run concurrently, `ensure_group`'s
`getent group || groupadd` (`workspaces/guest.py:107`, run by workspace create, grants and repair),
becomes `groupadd -f`, which succeeds when the group exists, as
`capabilities/vm_platform/bootstrap_script.py:107` already does. `retire_agent_socket_group`'s
check-then-delete (`sessions/tmux.py:542`) runs only under the vm exclusive and stays.

Serializing the tools is not enough for every tool. `gpasswd -d` copies the group's member list
before it takes the lock, then writes its copy back whole (shadow 4.13 `src/gpasswd.c:1016`, 4.17.4
`:933`, and `lib/commonio.c:1063` for the whole-record replacement), so two removals of different
agents from one group, each under its own grant, can each restore the member the other removed.
Membership removal (`agents/grants.py:283`, `workspaces/manager/repair.py:332`) therefore moves to
`usermod -r -G <group> <user>`, the same tool every addition already uses (`usermod -aG`). `usermod`
locks and loads the group file in `open_files` before `update_group` reads any membership from the
loaded copy (4.13 `src/usermod.c:1619-1630` and `:688-720`, the same order in 4.17.4), and `-rG`
removes only the named group. It exits 0 when the user is not a member and 6 (`E_NOTFOUND`) when the
user or the group does not exist (4.13 `:1078-1088`, `:1236-1239`; 4.17.4 `:1115-1126`,
`:1285-1287`), and in both cases the membership is already gone. `remove_from_workspace_group`
therefore treats 0 and 6 as removed instead of ignoring the status, and any other status, a
`lckpwdf` timeout (1 or 10) included, raises its typed error. So an agent whose account is gone (a
state agent create and reinit recover from, `agents/initializer.py:95-147`) can still lose its
grants, its workspaces and itself without a reinit first.

Recreating a missing account over its retained home (`_create_missing_agent_user`,
`agents/initializer.py:95-147`) is the one account edit that is two commands, `groupadd -g` then
`useradd -u -g` with the home's numeric IDs. Its comment (`:119`) relies on the VM guard. Under the
hierarchy it runs under its account's exclusive, and another account's creation on the VM can run
beside it; creation with an explicit ID refuses an ID already taken, so a collision fails with its
typed error and the retry hint it carries, and no two accounts ever share an ID. PR 2 updates that
comment to say so.

A caller that drops an agent's last grant on a workspace removes the membership first and deletes
the grant row only after the removal succeeds. Today session delete deletes its session and implicit
grant rows and then removes the membership best effort (`sessions/manager/_queries.py:249-262`), as
do revoke (`agents/grants.py:176-224`) and the session node (`sessions/nodes.py:286-291`); a failed
removal then leaves a member with no grant row to retry from. With the removal first, a failure
leaves the rows in place and rerunning the command retries it. The session node's rollback, which
removes a session it just created, keeps both the session and the grant row when the removal fails
and warns, so that the session can be deleted again.

### Agent account removal

`delete_agent_on_vm` (`agents/initializer.py:454-488`, reached from agent delete at
`agents/manager/lifecycle.py:435` and from agent create's rollback at `agents/realize.py:108`) keeps
today's `userdel -r` (`:485`), run under the account exclusive, and gains one check in front of it.
A rollback that removes an account its own command just created (agent create, and
`session create --new-agent` through `delete_agent` at `agents/nodes.py:219`) runs it under the set
that created the account, which already holds it exclusive, so the rollback is never refused.

1. **Stop.** `pkill -u <user>` and remove the socket directory, as today (`:473-483`).
2. **Check.** `pkill` only signals, so poll `pgrep -u <user>` for up to 5 s, counting only exit 0 as
   busy (exit 1 means none, exit 2 a missing user, as on a rerun). A process still alive refuses the
   removal, the outcome `userdel`'s own busy check gives today.
3. **Remove.** `userdel -r <user>`, unchanged. Exit 0, or 6 when the account is already gone, is
   success.

One outcome changes. Today any failure in these steps warns through the `STEP_FAILURES` handler
(`:486-488`) and agent delete still drops the agent's row (`agents/manager/lifecycle.py:435-437`).
Now a failure, a busy account or a `lckpwdf` timeout included, raises its typed error and agent
delete keeps the row, so rerunning the delete retries: the same rule as membership removal, where
cleanup succeeds before the row that tracks it is dropped. The two rollbacks differ. The
`session create --new-agent` rollback goes through `delete_agent` (`agents/nodes.py:219`), so the
agent has a row; its failure raises naming the agent left standing and the recovery command
(`:233-240`), and that command now finds the row it names. Agent create's rollback
(`agents/realize.py:103-112`) runs before any row exists and warns on failure, as today; a later
`agent create` of the name adopts the account left behind (`agents/initializer.py:248`).

Everything else keeps today's behavior. A `userdel-pre.d` hook or `userdel`'s busy check still
refuses before anything is removed, so a vetoed account keeps its home and mail spool. A non-forced
`-r` still keeps a mail spool the account does not own. The uid stays reserved while `userdel`
removes the home: in the shadow 4.13 sources `userdel` writes the account files only in
`close_files`, after `remove_mailbox` and `remove_tree`, and holds the passwd and group locks until
then. That matters because shadow gives a new account the next uid above the highest in use
(`libmisc/find_new_uid.c`), so a home that outlived its account's entry would belong to the next
`useradd` (`agents/initializer.py:83`), whatever its name; a removal that fails partway leaves what
it leaves today, and the rerun finds the account gone (exit 6) and drops the row.

The cost is those held locks. Today the VM guard serializes every account edit with an agent delete;
under the hierarchy an edit for another entity on the same VM can run beside one: agent create's
`useradd` (`agents/initializer.py:83`), a grant's `usermod -aG` (`agents/grants.py:262`), a
membership removal's `usermod -rG`, workspace create's `groupadd -f` and `usermod -aG`
(`workspaces/backends/vm.py:139`), another agent delete's or rollback's `userdel -r`. Each waits in
`lckpwdf`, and when the home takes longer than its 15 s to remove, the tool exits nonzero and raises
its typed error (`execution/access.py:139-142`). None loses state: an addition converges on rerun
(`usermod -aG`, `groupadd -f`), `useradd` fails before creating anything
(`agents/initializer.py:64`), and a removal keeps the row it would have dropped. Holding the vm
exclusive for agent delete would avoid the wait, but it would refuse access commands and unrelated
sessions for the whole removal, against FRD R2 and R4. Replacing `userdel -r` with a removal of our
own would avoid it too, at the price of reproducing shadow's vetoes and uid reservation in
agentworks; a rare, retryable timeout is the cheaper trade.

### Operation logs

`OperationLog` names its file `<vm>-<timestamp>-<stem>.log` with a one-second timestamp
(`execution/oplog.py:90`) and appends to it on every write (`:208-209`). Two operations with the
same stem on one VM that start in the same second, which the hierarchy now lets run together (two
`session-start` families, two `workspace-create`s of different workspaces), would interleave in one
file. The name gains a unique part, `<vm>-<timestamp>-<pid>-<6 hex>-<stem>.log`. The first write
creates the file with an exclusive open (`open(path, "x")`), drawing a new random suffix if the name
is taken, and later writes append as today. `vm_logs` (`oplog.py:46`) matches the new shape and
still the old one, so `vm delete` and `vm logs` find logs written before the change.

### SSH config rebuild

`agentworks/ssh_config_lock.py` holds `ssh_config_rebuild_lock()`, a per-OS-user exclusive file lock
at `CONFIG_DIR / ".locks" / "ssh-config"` (`config/validation.py:26`), taken with `native_locks`'
platform calls by polling the non-blocking try every 50 ms for at most 10 s. It lives outside
`native_locks` because it is not a native entity lock: it waits, it names no holder, and it guards a
workstation file. It is per OS user because the SSH config is that user's file whatever the
database. `sync_ssh_config` (`ssh_config.py:157`) holds it around its whole body, so its database
reads (`:436`, `:460`) and its writes (`:247`, `:363-381`) are one critical section and the last
rebuild always reads the latest committed state. A timeout raises `StateError` with the hint
`agw config sync-ssh-config`; callers keep their current failure handling, and
`sync_ssh_config_or_warn` turns it into its warning. Nothing inside it acquires a native lock.

## 6. Tests

New primitive tests live beside the package (`tests/native_locks/`), each marked
`pytest.mark.windows` so the Windows job (`.github/workflows/ci.yml:97-115`) runs it. Assertions
read fields (`entity_kind`, `entity_name`, mode, `holder.command`, `holder.held_for`,
`RuntimeResult.restore_intent`), never wording.

### PR 1, under today's lock

| Test                | Proves                                                                                                                                                                                                                                                                                                                         |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| account removal     | against a scripted guest: a surviving process after `pkill` and a 5 s poll refuses the delete and keeps the agent's row; `pgrep` exits 1 and 2 both proceed to `userdel -r`; a failing `userdel -r` raises and keeps the row, exit 6 drops it; a refusing `userdel-pre.d` hook leaves the home, the spool and the row in place |
| ensure_group        | two concurrent grants of different agents to one workspace whose group is missing both succeed, each creating or finding the group                                                                                                                                                                                             |
| membership removal  | `remove_from_workspace_group` runs `usermod -r -G` and checks its status: 0 and 6 (a non-member, a missing user or a missing group) count as removed, and any other status raises its typed error                                                                                                                              |
| removal before row  | when the membership removal fails, session delete, revoke and the session node keep the grant row (and session delete its session row), and a rerun removes the membership and then the rows                                                                                                                                   |
| delete spelling     | a workspace stored as `/front/link/`, a link to a non-empty directory, is deleted by removing the link; the directory and its contents remain                                                                                                                                                                                  |
| rebuild lock        | a second SSH config rebuild waits for the first and then reads the newer database state; a lock held past the bound raises the typed error                                                                                                                                                                                     |
| operation logs      | two logs for one VM and stem opened in the same second write separate files; a taken name draws another suffix; `vm_logs` finds the new and the old shapes                                                                                                                                                                     |
| formerly unguarded  | revoke, copy's source VM, `import-host-keys` (a per-VM `BUSY` outcome) and `confirm-release` each refuse while the VM's guard is held, as grant, repair and copy's destination already do                                                                                                                                      |
| prompts before lock | each moved prompt (the rows of Appendix A.1) is answered with no guard held, and its re-check refuses when the state it showed changed before acquisition                                                                                                                                                                      |
| rehome re-check     | rehome's step 5 persists nothing; a session started between its confirmation and its acquisition refuses the rehome                                                                                                                                                                                                            |
| spec start          | `session start --spec` asks its restart consent with no guard held, and refuses at step 7 when the session's status changed meanwhile                                                                                                                                                                                          |

### PR 2, the hierarchy

| Test                     | Proves                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| conflict matrix          | the four mode pairs on one entity, within a process (two sets) and across processes (a subprocess holder), on both platforms                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| tree                     | a session change refuses a workspace change and the vm exclusive, and is refused by them; sessions in different workspaces and accounts coexist; workspace delete and agent delete on a shared session refuse one another                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ordering                 | two contending acquisitions never both fail                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| probe                    | a conflicting probe raises naming the holder and writes no record; a free probe leaves nothing held; the probe never builds a `path` entity                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| crashed holder           | a killed subprocess holder frees every entity (bounded poll on Windows) and its record is removed by the next reader                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| old-version interlock    | a subprocess running today's algorithm inline (`flock` exclusive, or `msvcrt.locking` after `seek(0)`) refuses new shared and exclusive acquisitions with the fallback, and a new shared holder makes the old algorithm fail with its busy errno                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| holder naming            | the earliest conflicting live holder is named with its command, entity and age; a record with a free lock is deleted; a locked malformed one is skipped; non-conflicting records are ignored; a refusal during a holder's release finds no record and falls back; on POSIX a record a reader removes between its holder's create and lock is recreated, and a reader never unlinks a record whose holder has locked it; a refusal at a `path` entity names its resolved path; a holder whose record lock stays busy past 1 s runs without a record                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| operation label          | a command invoked through the CLI runner with a distinctive argument value records its command path and no part of that value; outside the CLI the label is `agw`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| nested reuse             | `require` acquires nothing; an uncovered or weaker entity, a different VM or database path, and a released set each raise `StateError`; the vm exclusive covers everything on the VM (the admin account's setup), while an account exclusive does not cover `grant/W+A` without `workspace/W` held; a console whose members changed still matches by key                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| prompt notice            | under a set acquired with `prompts=True`, every prompt helper and a resolver prompt show its notice exactly once, before the first prompt; under any other held set they raise `StateError`; with no set held they show nothing                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| activation               | a cold VM boots under a transient vm exclusive and the body holds the vm shared; no boot on a Tailscale miss when the provider reports the VM running; `auto_start` under a shared set refuses; step 7 refuses each compared field's change                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| recorded intent          | a family's or console's recorded intent survives a failure in its gate secrets, its provider observation and its boot; a start below a stopped-intent parent records intent and never prepares the VM                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| intent restore           | a family refused at (f) after recording intent reports blocked targets whose `restore_intent` is the `set-desired-state` command with the previous state, and an empty `restore_intent` when the previous state was a legacy NULL                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| family failure           | a refusal at (b), (d) or (f) at a session, console, workspace or account drops the targets that need it in the refused mode, the rest proceed, and a retry never adds a target: with session M held shared under console D's operation, a reconcile selecting M and stopped-intent console C, which contains M, drops M and reconciles C; a changed declaration fails its target alone; a busy vm, a failed provider observation, gate secret or boot fails the family; the invocation reports the others normally                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| account edit timeout     | against a scripted guest whose `userdel -r` holds shadow's lock past 15 s, a grant of another agent on that VM fails with its typed error carrying the tool's status, and a second agent delete fails the same way and keeps its row; each rerun after the removal succeeds; meanwhile a session attach and a session start of another agent on that VM are admitted                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| vm reconcile             | a `vm reconcile` whose VM target is running holds the vm shared, so an unrelated workspace create on that VM proceeds; with a stopped target it holds the vm exclusive                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| grant-all                | a workspace create and a grant-all agent create on one VM: one is refused before native work, and after a retry every grant-all agent has a grant for every workspace                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| bulk grant and revoke    | during `revoke-workspaces --all`, a named grant of a workspace the agent was not granted is refused; with no workspace on the VM, `--all` still holds the account shared, so an agent delete refuses it                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| workspace paths          | a rehome into `/data/B/new` and a rehome of `/data/B` refuse one another; a rehome of `/tree` without `--remove-old` and a child rehome inside it refuse one another; the walk records `/front`, `/middle` and `/physical` and the followed links `/front/link` and `/middle/bridge` for `/front/link/a` through `/front/link -> /middle/bridge -> /physical`, `/c`, `/e`, `/e/d` and `/f` and the followed links `/c/l1` and `/e/d/l2` for two links, and `/data`, `/p` and `/p/q` and the followed link `/data/x` for `/data/x/../y`, and a rehome of any recorded directory is refused meanwhile; a historical `/home/admin/../srv/ws1` walks and locks `/home`, `/home/admin` and `/home/srv`; more than 40 link expansions refuse; two creates of disjoint paths proceed together; a delete of a workspace whose directory is gone still deletes; a delete of a path whose final component is a dangling link or a cyclic one (`/w/loop -> /w/loop`) locks the entry `/w/loop` exclusive and `/w` shared, does not refuse, and removes only the link, while a rehome from that cyclic path refuses; a walk that changes by step 7 refuses; a rehome of `/front/link/child` through `/front/link -> /physical` and a delete of the workspace stored at `/front/link` refuse one another |
| native-home roots        | two `agent reinit` runs whose `CLAUDE_CONFIG_DIR` names one root outside both homes refuse one another, naming the holder; a root inside HOME takes no path lock; a predicted root that changes between step 5 and step 7 refuses; an `agent create` whose dotfiles export a root outside HOME, and an `agent reinit` whose re-synced dotfiles change it, each report the user setup incomplete naming the root and run no native CLI; a workspace rehome of a tree containing a held root is refused; retiring an integration from one agent's template while another agent's reinit holds their shared recorded root refuses the retiring reinit at acquisition, and a recorded root the set does not cover leaves the retirement incomplete and runs no native CLI                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| console lists            | two concurrent `console add-shell` calls on one member keep both shells; a restore whose pane plan changed under the final set and needs an unresolved secret refuses                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| restart declaration      | two restarts accepting overlays X then Y: the one whose accepted overlay was replaced refuses that target at (g)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| agent reinit declaration | a template or overlay changed after reinit's early save refuses it at step 7                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| family barrier           | no family takes its final set before every surviving family has finished (e)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| request validation       | two entities with one key and different parents, given as tuples, are refused before any keyed deduplication                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| prompt ownership         | two invocations in one process: a set one holds neither prints a notice for nor refuses a prompt of the other                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| rollback                 | a failing `session create --new-workspace --new-agent` unwinds without a refusal while unrelated holders exist on the VM                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| spec hand-off            | `session start --spec` hands the overlay to the batch, which persists it with the intent; spec takes no guard of its own and `run_session_batch(held=)` is gone                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |

One concurrent pair per FRD story, plus the session-delete pairs, driven through the real manager
functions with the scripted guest: the first operation is paused at a known guest command (an event
the fake guest waits on) while the second runs on another thread.

| Story                                         | Pair                                                                                    | Expect                                |
| --------------------------------------------- | --------------------------------------------------------------------------------------- | ------------------------------------- |
| sessions sharing neither workspace nor agent  | `session create` paused; `session start` of another session                             | both succeed                          |
| agent create or reinit beside other agents    | `agent reinit` paused; `session stop` of another agent's session                        | both succeed                          |
| workspace changes beside other workspaces     | `workspace delete` paused; `session start` in another workspace                         | both succeed                          |
| access during changes                         | `agent create` paused; `vm exec`, `session attach` and `session logs` admission         | admitted                              |
| VM-wide refused while something below changes | `session start` paused; `vm stop`                                                       | `vm stop` refused, holder named       |
| refusal names the holder                      | `workspace rehome` paused; `session start` in that workspace                            | refused naming rehome, workspace, age |
| session delete and its grant                  | `session delete` (agent mode) paused; `session create` for the same workspace and agent | create refused                        |
| session delete and revoke                     | `session delete` (agent mode) paused; `agent revoke-workspaces` of that workspace       | revoke refused                        |

PR 2's integration run also drives two account-editing operations at once on a trixie guest (for
example an agent create beside a workspace create, which run `useradd` and `groupadd`/`usermod -aG`
together) and confirms both succeed, proving the reliance on shadow's own serialization on the
release this LLD could not check on a host. It also removes two agents from one workspace group at
once (`usermod -r -G`, through two session deletes) on each supported release it has a guest for,
and confirms neither member remains, and reinitializes two agents whose native-home override names
one shared root outside both homes, confirming one is refused naming the other and a rerun succeeds.

### Harness setup confinement

Every harness integration's setup writes inside its owner entity, read at HEAD, or inside a path its
set holds:

- The vm facet publishes only under `ROOT_FILE_DIRECTORIES` (`/etc/claude-code`, `/etc/codex`,
  `/opt/agentworks/artifacts`; `artifacts/publication.py:30`, applied at
  `harness_setup/dispatch.py:318`), under the vm exclusive.
- The user facet's artifact files are confined to the account's HOME: `validate_user_placement`
  refuses any file outside it (`artifacts/native/common.py:85-91`, called at
  `plugins/claude/harness_integration.py:290`, `plugins/codex/harness_integration.py:576`,
  `plugins/grok/harness_integration.py:219`), and `publish_artifacts` takes the home as its only
  root (`dispatch.py:318-327`). MCP entries are confined to HOME too. Plugin installs run the native
  CLI as that account (`plugins/_harness_native/native_cli.py:470-473`, `:521`).
- **Native-home overrides, locked as paths.** The user layout honors a supported override of a
  native tool's home (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GROK_HOME`;
  `artifacts/native/layout.py:23-45`). It is observed, not declared: `resolve_user_layout` runs a
  login, interactive program as the account and reports what that login exports (`:52-66`, through
  the setup's own access, `harness_setup/lifecycle.py:291`). The settings writer publishes to that
  root (`plugins/_harness_native/native.py:317`), and plugin and marketplace setup run the native
  CLI against it (`plugins/_harness_native/native_cli.py:470-473`, `:521`), so two accounts can
  reconcile one writable root. Settings writes are compare-and-swaps
  (`artifacts/config_files.py:13-50`), but the native CLI's mutations are not, and today only the VM
  guard keeps two accounts' reconciliations apart. A root outside the account's HOME is therefore a
  `path` entity, held exclusive, as a workspace tree is. Because the login that reports it may not
  exist before the body (agent create and `session create --new-agent` create the account and
  install its dotfiles in the body, `agents/realize.py:131-150`, before `apply_agent_setup` at
  `:154-169`) and may change during it (agent reinit re-syncs dotfiles before its setup,
  `agents/manager/lifecycle.py:711`, then `:730`), the root is locked in two parts:
  1. **Predict at step 5.** The root is predicted from what is known before the body: the setup's
     declared environment once step 5 has resolved its values, the roots recorded in the agent's
     prior native claims (`read_native_setup`, `harness_setup/state.py:65`), which a retiring
     integration reconciles against (`plugins/_harness_native/native.py:310-314`), and for agent
     reinit also the current login's observation. Each predicted root outside HOME is walked as the
     admin and joins the request at step 6, and step 7 walks it again.
  2. **Check where the native CLI opens.** The one place a setup runs the native CLI against a real
     home is a non-isolated `NativeCLI` (`plugins/_harness_native/native.py:344`; the discovery home
     at `native_cli.py:512` is isolated in private scratch). Before it opens, a root outside HOME is
     walked and checked with `covers` against the held set
     ([Acquire, probe, require, release](#acquire-probe-require-release)), whichever path produced
     the root: `user_layout`'s result or a retiring integration's recorded root. A root the set does
     not cover is not reconciled: the setup reports itself incomplete through `report_incomplete`
     (`harness_setup/dispatch.py:265`), naming the root and `agw agent reinit` as the repair, whose
     step 5 then sees the new login or claims and locks it. `require` is not used here, because its
     miss is an interior contract violation (`StateError`) and this miss is an expected outcome.

  Two setups sharing a root then refuse each other, and a workspace operation on a tree containing
  the root is refused meanwhile. A root inside HOME is covered by the account's own exclusive and
  takes no path lock. The cost: an override set only through dotfiles leaves the first create's user
  setup incomplete until a reinit. The admin's setup runs under the vm exclusive and needs nothing
  more.

- The workspace facet publishes under the workspace root alone (`dispatch.py:318`): Claude's
  `.claude` and `.mcp.json` (`plugins/claude/harness_integration.py:341`,
  `plugins/_harness_native/native.py:200-214`), Codex's `.agents/skills` and `.codex/agents`
  (`plugins/codex/harness_integration.py:630-631`), Grok's `.grok`
  (`plugins/grok/harness_integration.py:270-276`), and the workspace's Git exclusions
  (`dispatch.py:330-331`).

Session-time writes reach the account's HOME under the account shared, and each is safe when two
sessions of one account launch at once: session artifacts go to a per-session directory
(`<home>/<AGENTWORKS_ARTIFACTS>/session/<session uuid>`, `artifacts/session.py:161-176`); Codex's
`session-home-skills` (`plugins/codex/artifacts.py:43-45`, `:164-169`) publishes into the shared
user skill catalog under package names derived from the session's uuid and run id
(`artifacts/native/guidance.py:53-56`), which keep sessions apart; and Codex's notify recorder is
one byte-identical atomic replace per launch (`plugins/codex/recorder.py:170-194`). No setup or
launch writer inside these rules crosses owners, so no request gains another owner.

### Existing tests that change

| PR  | File                                                                                                                                                                                                                                                                                                                                                    | Why                                                                                                                   |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| 1   | `tests/sessions/test_console_preparation.py` (`:139`), `tests/sessions/test_session_spec_launch.py`, `tests/vms/test_rekey_key_selection.py`, `tests/vms/test_recover.py`                                                                                                                                                                               | consent, delete confirmation and auth-key prompts move before the guard; recover's host-key confirm stays inside      |
| 1   | `tests/test_consoles_orchestration.py` (`:704`)                                                                                                                                                                                                                                                                                                         | `delete_console_record` (`sessions/multi_console/crud.py:609`), dead outside this test, is deleted rather than ported |
| 2   | `tests/capabilities/test_native_setup_locking.py`                                                                                                                                                                                                                                                                                                       | replaced by `tests/native_locks/`                                                                                     |
| 2   | `tests/vms/test_vm_nodes.py` (`:436-511`)                                                                                                                                                                                                                                                                                                               | `auto_start` and admission tests move to the sequence of section 2                                                    |
| 2   | `tests/sessions/test_console_preparation.py` (`:363`), `tests/runtime_state/test_failures.py`, `test_composition.py`, `test_preview_trust.py`                                                                                                                                                                                                           | the monkeypatched guard target moves; per-family probe and busy reporting; dry run still takes no lock                |
| 2   | `tests/test_session_ephemeral_guard_unwind.py`, `tests/sessions/test_create_start_restart_orchestrated.py`, `tests/agents/test_create_reinit_orchestrated.py`, `tests/vms/test_lifecycle_orchestrated.py`                                                                                                                                               | held set instead of the guard token; acquisition after the gate                                                       |
| 2   | `tests/capabilities/test_native_setup_lifecycle.py`, `test_native_setup_deletion.py`, `test_native_setup_state.py`                                                                                                                                                                                                                                      | `held=` becomes `held.require` with the owner's request                                                               |
| 2   | `tests/sessions/test_prepared_runtime.py`, `test_console_value_barrier.py`, `test_console_attach_orchestrated.py`, `tests/artifacts/test_session_lifecycle.py`, `tests/test_wsl2_keepalive.py`, `tests/vms/test_reinit_admin_options.py`, `tests/vms/test_delete_vm_gating.py`, `tests/test_legacy_claude.py`, `tests/orchestration/test_activation.py` | construct or patch the old guard; mechanical port                                                                     |

## 7. Collateral

Every item states the current single-guard behavior. Each PR updates what it outdates; the HLA's
delivery list names the same set.

- PR 1: the sentences saying secret prompts and console delete confirmation keep a family busy
  (`cli/command-reference.md:689-690`, `docs/guides/desired-runtime-state.md:161-163`,
  `cli/agentworks/guide/guide-content/troubleshooting.md:75-76`), and the operation-log name in
  `execution/oplog.py`'s module docstring.
- PR 2, everything else:
  - `cli/command-reference.md:687-692`, the busy paragraph: hierarchical sets, what refuses what,
    the holder message, the three prompting exceptions and the notice line, admission still not
    spanning a body.
  - `cli/agentworks/orchestration/README.md:166-170`: held sets replace the family guard and token;
    the operation sequence.
  - the upgrade guide of the release PR 2 ships in (0.20.0 ships before it): operations below the VM
    now run concurrently, a refusal names its holder, and an older agw still running during the
    upgrade interlocks safely but reports busy without naming holders.
  - `docs/guides/desired-runtime-state.md:154`, `:163-165`.
  - `docs/guides/native-harness-setup.md:336-340`, `docs/guides/harness-facets.md:400`,
    `docs/guides/idempotency.md:298`, `cli/agentworks/db/README.md:138`,
    `cli/agentworks/capabilities/harness_integration/README.md:376`: "VM-family mutation guard"
    becomes the owner's lock.
  - `cli/agentworks/guide/guide-content/troubleshooting.md:55-77`, a guide topic: busy causes and
    the holder message.
  - The `native_locks` package docstring carries the durable rules (tree, modes, order, no upgrade,
    read then lock the whole request, records advisory) so they outlive this SDD.

No sample-config or completion change: the waits are constants and the command tree is unchanged.

## 8. Delivery: two stacked PRs

**PR 1, under today's lock.** Everything that is right whatever the lock's shape, landed while
`native_mutation_guard` still serializes each VM, so each change is reviewed and proved on its own:

- `ensure_group` uses `groupadd -f`; membership removal uses `usermod -r -G` with its status checked
  and runs before the last grant row is deleted; workspace delete removes its normalized path; agent
  removal polls `pgrep -u` after `pkill`, and a busy account or a failed `userdel -r` raises and
  keeps the agent's row ([Agent account removal](#agent-account-removal)), while agent create's
  rollback, which runs before any row exists, still warns.
- The SSH config rebuild lock, and the bounded dpkg wait in workspace repair.
- Unique operation-log names.
- Today's guard on the mutators still unguarded at HEAD: revoke, workspace copy's source VM (beside
  the destination guard #956 added, both in VM name order), `vm import-host-keys` (with its `BUSY`
  outcome) and `vm confirm-release`. Grant, repair and copy's destination already take it
  (`agents/grants.py:97`, `workspaces/manager/repair.py:133`, `workspaces/manager/copy.py:169`).
- Rehome's pre-confirmation sessions check becomes pure observation, with its PID repair under the
  guard.
- Workspace delete keeps its row when `rm -rf` or `groupdel` fails (`groupdel` exit 6 counts as
  removed), so a rerun retries, as agent delete does.
- Prompts before acquisition: every row of Appendix A.1 except those it moves to PR 2, with the
  step-7 re-checks of what each prompt showed, `session start --spec`'s consent, and the deletion of
  `delete_console_record`. The empty-console offer joins the workspace and agent offers after the
  guard is released.

**PR 2, the hierarchy.** The `native_locks` package replaces `harness_setup/locking.py` in one step:
entities and requests at every site of Appendix A.2 (with the bulk grant pairs, the `path` entities
and the guest lookup walk, and the console list rule), the probe, the brief boot exclusive and
shared admission, the runtime families' intent acquisition with the prompt moves Appendix A.1 defers
to it, and the per-target retry rule with `restore_intent`, holder records and the R3 message, the
per-invocation structural prompt notice with `prompts=True`, the restart and agent reinit
declaration checks, and the family barrier.

## Appendix A: implementation checklist

### A.1 PR 1: prompts to move before the guard

| Command                                                                      | Under the guard today                                                                                                                                                                                                  | Moves to                                                              |
| ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `vm reinit`                                                                  | gate and boundary secrets (`vms/manager/reinit.py:315`, `:331`), guard `:71`                                                                                                                                           | steps 4 and 5                                                         |
| `vm delete`                                                                  | boundary secrets (`vms/manager/boundary.py:289`), guard `power.py:218`                                                                                                                                                 | step 5                                                                |
| `vm rekey`                                                                   | auth key and `--prompt` (`boundary.py:346`, `:361`), guard `power.py:412`; `--wait-for-share` stays (`:490`)                                                                                                           | step 5                                                                |
| `vm recover`                                                                 | secrets (`boundary.py:361`), guard `vms/manager/recover.py:84`; host-key confirm stays (`recover.py:240`)                                                                                                              | step 5                                                                |
| `vm confirm-release`                                                         | none; its confirm (`vms/manager/release.py:79`) precedes the new guard                                                                                                                                                 | step 5                                                                |
| `console attach`                                                             | `require_vm_ssh_boundary`, gate secrets (`prepare_vm_runtime`) and a possible boot (`activate_vm_runtime`), all under the guard at `sessions/multi_console/lifecycle.py:257` until `:303`, before the attach at `:304` | gate secrets to step 4; the boot moves out of the guard in PR 2       |
| `agent delete`                                                               | boundary secrets, guard `lifecycle.py:331`; its session list is read before the guard (`:307`)                                                                                                                         | step 5; step 7 compares the session names                             |
| `workspace delete`                                                           | boundary secrets (`workspaces/manager/delete.py:134-142`), guard `:106`                                                                                                                                                | step 5                                                                |
| `workspace rehome`                                                           | sessions check, which also persists a PID repair (`workspaces/manager/rehome.py:101-123`), confirm (`:217`), secrets (`:181-189`), guard `:96`                                                                         | step 5 as pure observation; the repair and the check re-run at step 7 |
| `session create`                                                             | gate and boundary secrets (`sessions/manager/_create.py:414-417`), guard `:377`                                                                                                                                        | steps 4 and 5                                                         |
| `session start --spec`                                                       | restart consent (`sessions/manager/_spec.py:133-135`), guard `:128`; its overlay hand-off to the batch is PR 2's                                                                                                       | step 5                                                                |
| `session delete`                                                             | confirm (`sessions/manager/_queries.py:227`), secrets, guard `:154`; the empty-console offer (`:295`, `sessions/_resource_cleanup.py:97`) moves after the guard is released                                            | step 5                                                                |
| `console create`                                                             | gate and pane secrets (`sessions/multi_console/attach.py:453`, `:495`), guard `multi_console/crud.py:163`                                                                                                              | steps 4 and 5                                                         |
| `console delete`                                                             | confirm (`multi_console/attach.py:856`), guard `:854`                                                                                                                                                                  | step 5                                                                |
| `console add-sessions`, `add-shell`                                          | pane secrets (`crud.py:379`, `:696`), guards `:325`, `:648`                                                                                                                                                            | step 5; console row and each added session re-checked at step 7       |
| `console remove-sessions`                                                    | guard `:466`; the empty-console offer (`crud.py:494`) moves after the guard is released                                                                                                                                | step 5                                                                |
| `console restore-session`                                                    | gate and pane secrets (`multi_console/restore.py:353`), guard `:293`                                                                                                                                                   | steps 4 and 5                                                         |
| `agent grant-workspaces`, `revoke`, `workspace repair`, `copy`'s destination | gate secrets, resolved inside the guards #956 added and PR 1 adds (`agents/grants.py:97`, `:130`; `workspaces/manager/repair.py:133`; `copy.py:169`)                                                                   | step 5, after the gate, with step 7's re-check                        |

**Moved to PR 2.** These rows write intent or a declaration that must precede their prompts, under a
brief acquisition of their own (section 2's intent acquisition for runtime families, and agent
reinit's brief account exclusive). Under today's single guard that would mean building the two-phase
sequence twice, so they move with that acquisition. `session start --spec`'s overlay hand-off to the
batch and the removal of `run_session_batch(held=)` depend on the batch row and move with it.

| Command                                            | Under the guard today                                                                                               | Moves to                                         |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| `vm start`, `stop`                                 | gate secrets (`vms/manager/runtime.py:73`), guard `vms/manager/power.py:74`, `:118`                                 | step 5, after the intent acquisition             |
| `agent reinit`                                     | gate and boundary secrets (`agents/manager/lifecycle.py:621`, `:664`), guard `:519`; the early template save `:587` | steps 4 and 5, after the brief account exclusive |
| `console start`, `restart`, `stop`                 | gate secrets, guard `sessions/multi_console/lifecycle.py:69`, `:149`; pane secrets stay                             | step 5, after the intent acquisition             |
| `vm reconcile`, session batch, `console reconcile` | gate secrets (`runtime_state/_prepare.py:219`), guard `runtime_state/_selection.py:101`; workload union stays       | runtime path (e)                                 |

### A.2 PR 2: acquisition sites

| Command                                                                         | Acquires today                                                                                                                                                                                                                                           |
| ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `vm create`                                                                     | `vms/manager/lifecycle.py:383` (by name)                                                                                                                                                                                                                 |
| `vm reinit`, `start`, `stop`, `delete`, `rekey`, `recover`, `set-desired-state` | `reinit.py:71`; `power.py:73`, `:117`, `:218`, `:412`, `:36`; `recover.py:84`                                                                                                                                                                            |
| `vm reconcile`, session batch, `console reconcile`                              | `runtime_state/_selection.py:101`, families in VM name order                                                                                                                                                                                             |
| `vm import-host-keys`, `vm confirm-release`                                     | PR 1's new guards (`vms/manager/host_keys.py:126-149`, `vms/manager/release.py:86-89`)                                                                                                                                                                   |
| access commands                                                                 | admission only: `vms/manager/exec.py:121`, `:193`, `:278`; `port_forward.py:80`; `vms/backup.py:81`; `agents/manager/access.py:143`, `:244`; `sessions/manager/_queries.py:938`; `_logs.py:44`; `sessions/multi_console/lifecycle.py:257` (until `:303`) |
| `agent create`, `reinit`, `delete`                                              | `agents/realize.py:119`; `agents/manager/lifecycle.py:504`, `:331`                                                                                                                                                                                       |
| `agent grant-workspaces`, `revoke`                                              | grant's guard from #956 (`agents/grants.py:97`); PR 1's new guard on revoke (`:130`)                                                                                                                                                                     |
| `workspace create`, `delete`, `rehome`                                          | `workspaces/realize.py:124`; `workspaces/manager/delete.py:106`; `rehome.py:96`                                                                                                                                                                          |
| `workspace repair`, `copy`                                                      | the guards #956 added on repair and copy's destination (`workspaces/manager/repair.py:133`, `copy.py:169`); PR 1's new guard on copy's source                                                                                                            |
| `session create`, `set-desired-state`, `delete`                                 | `sessions/manager/_create.py:377` (and the slice `_create_roll.py:248`); `_intent.py:59`; `_queries.py:154`                                                                                                                                              |
| consoles                                                                        | `multi_console/crud.py:163`; `guarded_console_selection` (`multi_console/_helpers.py:221-240`) at `lifecycle.py:69`, `:149`, `:257`, `attach.py:854`, `runtime.py:132`, `crud.py:325`, `:466`, `:525`, `:648`, `restore.py:293`                          |
| nested                                                                          | `harness_setup/dispatch.py:116`; `harness_setup/lifecycle.py:262`, `:312`, `:365`; `vms/initializer/driver.py:187`; `vms/nodes.py:242`, `:279`                                                                                                           |
