# Native lock hierarchy: low-level design

<!-- cspell:ignore contextmanager contextvar ctypes wintypes inodes -->
<!-- cspell:ignore argtypes restype LPOVERLAPPED dpkg -->

Status: draft. Builds on the accepted [FRD](./frd.md) and the [HLA](./hla.md). Code citations are
`file:line` under `cli/agentworks/` at 27b215e9b unless a path says otherwise.

This document fixes the primitive, the one sequence every operation follows, the holder records, the
lock set of every command, the mechanism for each piece of shared state, the tests and the
collateral. [Appendix A](#appendix-a-implementation-checklist) maps each command's current
acquisition site to its change.

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
console has the vm among its parents; a session's parents are exactly one workspace and one account;
a grant's are one workspace and one account; a console's are the vm and its member sessions; every
parent belongs to the same vm.

| Constructor                                  | Display key                   | Notes                                           |
| -------------------------------------------- | ----------------------------- | ----------------------------------------------- |
| `vm_entity(vm)`                              | `vm/<vm>`                     | the existing vm file                            |
| `workspace_entity(vm, name)`                 | `workspace/<name>`            |                                                 |
| `account_entity(vm, linux_user)`             | `account/<linux user>`        | `AgentRow.linux_user` or `VMRow.admin_username` |
| `grant_all_entity(vm)`                       | `grant-all`                   | the VM's set of grant-all agents                |
| `grant_entity(workspace, account)`           | `grant/<workspace>+<account>` | one account's membership in one workspace group |
| `session_entity(name, workspace=, account=)` | `session/<name>`              |                                                 |
| `console_entity(vm, name, members=)`         | `console/<name>`              | members are session entities                    |

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

### Modes and requests

```python
class LockMode(IntEnum):
    SHARED = 1
    EXCLUSIVE = 2


@dataclass(frozen=True)
class LockRequest:
    exclusive: frozenset[Entity] = frozenset()
    shared: frozenset[Entity] = frozenset()

    def modes(self) -> Mapping[Entity, LockMode]: ...
```

`modes()` gives every entity in `exclusive` the exclusive mode and every other entity in `shared` or
in the ancestor closure of either set the shared mode. All entities of one request name one VM, and
the constructor refuses otherwise; a command that spans VMs holds one set per VM. A request whose
only entity is the vm, shared, is an access command's admission request. The same entity appearing
twice with different parents is refused, since it means two reads disagreed.

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
def native_locks(database_path: Path, request: LockRequest) -> Iterator[NativeLockSet]: ...


def probe_locks(database_path: Path, request: LockRequest) -> None: ...


class NativeLockSet:
    database_path: Path
    vm: str

    def covers(self, entity: Entity, mode: LockMode) -> bool: ...
    def require(self, request: LockRequest) -> None: ...
    def announce_prompt(self) -> None: ...
```

- **Acquire** once: open each file of `request.modes()` in order and try its mode without waiting.
  The first refusal releases everything this call took, then raises `NativeLockBusyError`. On
  success the set writes its holder record ([section 3](#3-holder-records)) and is yielded; on exit
  it releases every entity, then removes the record. A set never grows: every operation builds its
  whole request from a read made before locking ([section 4](#rules)).
- **Probe**: the same acquisition released at once, writing no record. It lets a command fail on a
  conflict before it prompts or boots; it decides nothing, since the real acquisition follows.
- **Require** (nested reuse, replacing today's `held=`): a nested operation states the request for
  what it changes and calls `held.require(request)`, which checks the set is active and for the same
  database and VM (as `harness_setup/locking.py:46-48` does today) and that it covers every entity
  of the request in at least the requested mode. Coverage compares keys and modes, never parents, so
  a nested console delete after a session's membership cascade still matches. Missing coverage
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
        holders/<pid>-<16 hex>.json            holder records
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
and each with a deadline.

### Why not an existing lock

Two locks already exist and neither fits. `db/backup.py:477-543` gives shared and exclusive locks
through SQLite transactions on side files; it would put sub-VM entities on a different mechanism
from the vm level, which must stay `flock` and `LockFile` on today's byte to interlock with older
releases, and it costs a SQLite file and connection per entity for a one-byte question. The guest
file program's lock (`execution/file_program.py:41`) lives on the guest and waits up to 30 s; it
makes one file operation consistent among identities on one VM, not one workstation operation
exclusive against another, and it cannot fail fast or name a holder.

## 2. The operation sequence

Every command that changes native state runs one sequence. It honors R4 (no operation below the VM
holds the vm exclusive beyond a boot) and R6 (no prompt under a lock but the three named below):

1. **Read** what the operation depends on, without locks, and build its whole request.
2. **Probe** the request (`probe_locks`): a conflict fails here, naming its holder, before any
   prompt or boot.
3. **Ask and look**: resolve gate secrets and the boundary's secrets, run read-only guest probes,
   ask confirmations. Nothing is locked.
4. **Boot** if the operation needs the VM running and `vm_node.confirmed_active()`
   (`vms/nodes.py:196-208`) is false: take `{vm: exclusive}` alone, run `auto_start` under it
   (`nodes.py:236-271`, which re-reads the row and the provider status and starts only a stopped
   VM), and release it. Stop-only operations never boot.
5. **Acquire** the request once, with the vm shared.
6. **Re-check**: `vm_node.hold_active(held, snapshot)` re-reads the VM row under the set and refuses
   with `StateConflictError` if its vm_uuid, desired runtime state or Tailscale host differ from
   what step 3 saw (a `vm stop`, `delete`, `rekey` or `recover` ran in between), then enters
   `vm_active` unless intent is stopped, as `nodes.py:282-284` does today. Stop-only paths skip the
   hold, which could boot an off VM (`orchestration/README.md:193-194`). The command re-reads its
   own step-1 rows and re-runs its step-3 probes; any difference refuses with `StateConflictError`
   ("changed since this command read it", hint "Run the command again.").
7. **Run** the body; nested operations call `held.require`.

`gated_vm_boundary` (`vms/manager/boundary.py:192`) and `activation_gate`
(`orchestration/activation.py:158`) become this sequence. The boundary takes the request, runs steps
2 to 3 on entry, and yields an object whose `locked()` runs steps 4 to 6 and yields the held set, so
a command runs its probes and confirms between the two. With the vm already confirmed active, step 4
does nothing, and step 6's `hold_active` is the one VM re-check.

**Access and observe commands** (`vm shell`, `vm exec`, `vm port-forward`, `vm backup`,
`agent shell`, `agent exec`, `session attach`, `session logs`, `console attach`) run steps 2 to 4
with the admission request, then `admit()`: take `{vm: shared}`, run `hold_active`'s re-check,
release, and run the body unlocked with the `vm_active` keepalive held for the span, as today
(`nodes.py:285`, `cli/command-reference.md:690-691`).

**VM-wide commands** request `{vm: exclusive}` itself; steps 4 and 5 merge, and `auto_start` runs
under the held set.

**Runtime families and console runtime commands record intent before activation** (`runtime_state`
for `session start`, `stop`, `restart`, `reconcile` and `console reconcile`; console start, restart
and stop; `session start --spec`). Today a start's intent survives a failed activation
(`tests/sessions/test_console_preparation.py:307`, and `:326` records a child's intent under a
stopped parent without activating). They keep that ordering inside the same sequence:

- A family that does not boot (its VM is confirmed active, or it only stops) acquires its set at
  step 5 and records intent at step 7, before anything activates.
- A family that needs its VM running (`runtime_state/_prepare.py:167`) and finds it not confirmed
  active records intent at step 4, under the boot's `{vm: exclusive}`, which covers every entity on
  the VM, then activates and releases. Step 6 re-checks the selection (the existing uuid and route
  checks, `runtime_state/_selection.py:103-149`) and that the recorded intent still stands, refusing
  that target otherwise.
- Under the family's shared vm, a power action that turns out to be needed (the VM went down after
  step 4) refuses with
  `StateError("VM '<name>' stopped after this command checked it", hint="Run the command again.")`;
  activation calls `held.require({vm: exclusive})` first.
- `vm reconcile` and other explicit VM selections are VM-wide: their family requests the vm
  exclusive for the run.

Each family is probed, booted and acquired on its own, in VM name order; a conflict or failure at
any step fails that family alone and the others proceed, as `_selection.py:122-128` does today.

### Prompts under a set

Three prompts stay inside a held set because they ask about what the locked operation is doing:
`vm recover`'s host-key confirmation (`vms/manager/recover.py:240`), `vm rekey --wait-for-share`
(`vms/manager/power.py:490`), and workload and console pane secrets, whose set depends on
definitions and observations made under the lock (`runtime_state/_execute.py:45`,
`sessions/multi_console/lifecycle.py:117-118`, `crud.py:379`, `:696`, `restore.py:353`). The first
two hold only the vm, exclusive anyway; the third holds only its family's or console's set.

Before such a prompt the command calls `held.announce_prompt()`, which prints one `output.info` line
naming the held entities and saying they stay locked until the answer is given, once per set. The
prompt helpers (`output.confirm`, `choose`, `pause`, `prompt`, `prompt_secret`, `output.py:618-640`,
which the resolver's terminal entry uses) call `native_locks.check_prompt()`, which raises
`StateError` when any set this process holds has not announced. That is an interior contract, so a
prompt path that forgot the notice fails the test suite rather than silently prompting under a lock.

## 3. Holder records

### Format and location

One JSON file per held set, `<sha256(vm)>.d/holders/<pid>-<16 hex>.json`, mode 0600:

```json
{
  "version": 1,
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
  content, so readers can still read it), and writes the JSON. The handle and its lock live as long
  as the set.
- On release the holder releases every entity lock, then unlocks, closes and unlinks the record. On
  Windows the unlink can fail while a reader has the file open; that failure is ignored, and the
  next reader finds the record unlocked and removes it.

### Stale records

A record is live exactly while its own lock is held, which the OS releases when the holder dies. A
reader tests liveness by trying a shared lock on the record without waiting (same offset on
Windows). Success on a record that parses means the holder is gone: the reader unlocks, closes the
file and only then unlinks it (Windows cannot remove a file the reader holds open), ignoring a
failed unlink. A record that does not parse is neither named nor deleted while locked; unlocked, a
holder may be between creating the file and locking it, so it is deleted only once its modification
time is more than a minute old. No pid is probed, so a reused pid cannot make a dead holder look
alive.

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

Argument or option values, secret names or values, environment, paths, hostnames, prompt answers, or
anything else read from the operator. Entity names are identifiers already in the database beside
it, under the same 0700 directory and the same OS user. The only fields are the ones above.

## 4. Lock sets per command

### Rules

- **Secrets and confirmations come before acquiring** (step 3), except the three prompts of
  [Prompts under a set](#prompts-under-a-set). A confirmation that shows guest state runs its
  read-only probes unlocked first and again under the set.
- **Read, lock once, re-check.** The request is built from a step-1 read: the dependents an
  operation will change (an agent's sessions, grants and consoles; a workspace's; the grant-all
  agents a workspace create will grant) are all in it, and all of it is read before any native
  change. After acquiring, the operation re-reads; if the rows, memberships or sets it read changed,
  or no longer match what a confirmation showed (the session count of an agent or workspace delete),
  it refuses with `StateConflictError` before any native change.
- **Nested operations require.** `realize_workspace`, `realize_agent`, harness setup
  (`harness_setup/dispatch.py:116`, `harness_setup/lifecycle.py:262`, `:312`, `:365`), teardown
  deletes and the empty-console delete call `held.require` with their own request, which their
  caller's request covers.
- Dry runs take no lock, as today.

### Lock sets

X is taken exclusive; every ancestor of an X entity is taken shared and not listed; S lists further
shared entities. A console's ancestors are the vm and its member sessions with their workspaces and
accounts.

| Command                                                                                                                                                         | X                                                                                                                                                              | S                                                   |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| `vm create` (by name), `reinit`, `start`, `stop`, `delete`, `rekey`, `recover`, `set-desired-state`                                                             | vm                                                                                                                                                             |                                                     |
| `vm import-host-keys`, `vm confirm-release`                                                                                                                     | vm (per VM)                                                                                                                                                    |                                                     |
| `vm reconcile`                                                                                                                                                  | vm, per family                                                                                                                                                 |                                                     |
| access and observe commands                                                                                                                                     | none: admission only                                                                                                                                           | vm                                                  |
| `agent create`                                                                                                                                                  | account; with `--grant-all-workspaces` also grant-all and a grant per workspace on the VM                                                                      |                                                     |
| `agent reinit`                                                                                                                                                  | account, a grant per recorded grant it replays (`agents/manager/lifecycle.py:783`)                                                                             |                                                     |
| `agent delete`                                                                                                                                                  | account, each of its sessions, grants and consoles holding them; grant-all when the agent is grant-all                                                         |                                                     |
| `agent grant-workspaces`, `revoke-workspaces`                                                                                                                   | a grant per named workspace; `--all`: grant-all and a grant per workspace on the VM (grant) or per granted workspace (revoke)                                  |                                                     |
| `workspace create`                                                                                                                                              | workspace, grant(workspace, admin), grant(workspace, agent) per grant-all agent                                                                                | grant-all                                           |
| `workspace delete`                                                                                                                                              | workspace, each of its sessions, grants and consoles holding them                                                                                              |                                                     |
| `workspace repair`                                                                                                                                              | workspace, grant(workspace, admin), a grant per agent holding one on it                                                                                        |                                                     |
| `workspace rehome`                                                                                                                                              | workspace                                                                                                                                                      |                                                     |
| `workspace copy`                                                                                                                                                | destination VM: workspace, grant(workspace, admin), grant per grant-all agent                                                                                  | destination: grant-all; source VM: source workspace |
| `session create`                                                                                                                                                | session; grant(workspace, agent) in agent mode; `--new-workspace` adds workspace, grant(workspace, admin) and the grant-all grants; `--new-agent` adds account | `--new-workspace`: grant-all                        |
| `session start`, `stop`, `restart`, `reconcile`                                                                                                                 | each selected session, per family                                                                                                                              |                                                     |
| `session set-desired-state`                                                                                                                                     | session                                                                                                                                                        |                                                     |
| `session delete`                                                                                                                                                | session; grant(workspace, agent) in agent mode; each console holding it                                                                                        |                                                     |
| `console create`, `start`, `stop`, `restart`, `reconcile`, `set-desired-state`, `delete`, `remove-sessions`, `reorder-sessions`, `add-shell`, `restore-session` | console                                                                                                                                                        |                                                     |
| `console add-sessions`                                                                                                                                          | console                                                                                                                                                        | each added session                                  |
| `vm describe`, `vm verify-connection`, `vm logs`, every `describe` and `list`                                                                                   | none                                                                                                                                                           |                                                     |

### Rows that need more than a line

**Grant-all.** Workspace create (and copy's destination, and `session create --new-workspace`) reads
the grant-all agents at step 1, requests a grant for each, and re-reads them under its shared
`grant-all`; agent create with grant-all and grant or revoke `--all` read the workspaces at step 1
and re-read them under their exclusive `grant-all`, which no concurrent workspace create can hold.
Both sides therefore finish their reads before any native change.

**Workspace copy across two VMs.** One set per VM, probed and acquired in VM name order, one set
when both are the same VM. The source workspace is held shared so it cannot be deleted, rehomed or
repaired mid-copy; sessions in it keep running. Copy's destination prompt (`_resolve_vm`,
`workspaces/manager/_common.py:117`, reached from `copy.py:140`) is step 3.

**Session delete.** In agent mode it may remove the implicit grant and the group membership
(`sessions/manager/_queries.py:253-262`), so it holds that grant exclusive, which refuses a
concurrent `session create` for the same pair and a `revoke-workspaces` of it. Removing a console
member changes the console, so it holds each console holding the session exclusive, with the
console's other members shared. Its confirmation (`_queries.py:227`) and the empty-console offer are
step 3; the offer is phrased as "will be empty", and under the set a consented console is deleted
only if it is still empty, while one that became empty without an offer is kept with a note. The
workspace and agent follow-on offers (`_queries.py:159-160`) run after the set is released; the
deletes they trigger run their own sequence, whose re-check refuses if the workspace or agent is no
longer empty.

**`vm import-host-keys`.** Today it takes no lock. Each VM's `_import_one`
(`vms/manager/host_keys.py:126-149`) runs under that VM's `{vm: exclusive}`; a refusal is a new
per-VM `BUSY` outcome carrying the R3 message, the other VMs carry on, and the final SSH config sync
still runs.

**`vm confirm-release`.** Today its database writes take no lock and no re-check
(`vms/manager/release.py:86-89`). Its confirmation (`:79`) is step 3; it then takes
`{vm: exclusive}`, re-reads the row's uuid and the observed release, and writes.

**Session create.** Plan prompts and graph build are step 1; `require_vm_ssh_boundary`, the gate and
`_preflight_and_resolve` (`sessions/manager/_create.py:413-417`) are steps 3 to 4; one acquisition
covers the session, grants and any new workspace or account; the existing VM reload
(`_create.py:378-386`) joins step 6. The held set is attached to the graph so `realize_workspace`,
`realize_agent` and the session slice `require` it. Rollback runs inside the set:
`PendingAgentNode.teardown` and `PendingWorkspaceNode.teardown` call `delete_agent` and
`delete_workspace` (`agents/nodes.py:219`, `workspaces/nodes.py:151`), whose requests name only this
create's own account or workspace, session and grants, all already in the set, so a rollback is
never refused.

**Workspace rehome.** Step 3 runs its not-stopped-sessions check (today under the guard,
`workspaces/manager/rehome.py:101-123`), its directory probes (`:191-206`) and its confirmation
(`:217`), which shows only the stored and new paths. Under `workspace` exclusive, step 6 re-reads
the stored path and runs the sessions check and the probes again, since a session can start between
the two; the target is then reserved with the exclusive `mkdir` it uses today (`:228`).

## 5. Shared state across entities

| State                                    | Mechanism                                                                                                                                                                                                                                                                      |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| VM account and group files               | shadow-utils' own serialization, below; `ensure_group` made idempotent                                                                                                                                                                                                         |
| an agent's home and mail spool           | removed outside shadow's locks, under the account's exclusive ([Agent account removal](#agent-account-removal))                                                                                                                                                                |
| `/opt/agentworks/tmux-session.conf`      | already an atomic replace: `deploy_restricted_config` (`sessions/tmux.py:711-722`) writes through `Files.write`, an `O_EXCL` temporary with a fresh name renamed over the file (`execution/file_program.py:298-328`), whose parents tolerate a concurrent `mkdir` (`:126-129`) |
| socket roots and per-account socket dirs | already convergent: each step of `ensure_agent_socket_root`, `ensure_agent_socket_dir` and `ensure_admin_socket_root` (`tmux.py:456-511`, `:607-632`) is `mkdir -p` or an absolute `chown`/`chmod`, so any interleaving ends in the same state                                 |
| stale socket cleanup                     | `cleanup_stale_sockets` (`tmux.py:546`) runs only from agent create and reinit (`agents/initializer.py:227`, under the account exclusive) and vm create and reinit (`vms/initializer/driver.py:349`, under the vm exclusive); a launch holds its account shared                |
| workspace group membership               | the grant entity                                                                                                                                                                                                                                                               |
| the grant-all agents and the workspaces  | the grant-all entity                                                                                                                                                                                                                                                           |
| a workspace directory path               | already reserved by an exclusive `mkdir` in create (`workspaces/backends/vm.py:127`) and rehome (`rehome.py:228`)                                                                                                                                                              |
| console membership                       | the console entity, with its members as parents                                                                                                                                                                                                                                |
| the dpkg lock                            | workspace repair's `acl` install (`workspaces/manager/repair.py:158`) gains `-o DPkg::Lock::Timeout=120`, as `vms/recovery.py:117-118` uses, since two repairs of different workspaces can now run at once                                                                     |
| operator SSH config                      | the SSH config rebuild mutex ([below](#ssh-config-rebuild))                                                                                                                                                                                                                    |
| host-key store                           | per-VM file written atomically (`execution/trust.py:242-261`) under the vm exclusive                                                                                                                                                                                           |

VS Code workspace files are written and removed per workspace inside workspace create and delete
(`workspaces/backends/vm.py:350`, `workspaces/manager/delete.py:263`), under that workspace's
exclusive, and by `vm delete` (`vms/manager/power.py:308`) under the vm exclusive. Their bulk
regeneration (`cli/commands/config.py:80`) already races today and is tracked as a separate issue.

### Account files

No agentworks lock wraps the account and group edits. Both supported guests (`debian.py:26-27`:
bookworm and trixie) ship shadow-utils that serializes its own tools through glibc's `lckpwdf`,
which waits up to 15 s: bookworm's shadow 4.13 (on this host, `useradd` links `lckpwdf`) and
trixie's 4.17.4. The one check-then-act that can now run concurrently, `ensure_group`'s
`getent group || groupadd` (`workspaces/guest.py:107`, run by workspace create, grants and repair),
becomes `groupadd -f`, which succeeds when the group exists, as `bootstrap_script.py:107` already
does. `retire_agent_socket_group`'s check-then-delete (`sessions/tmux.py:542`) runs only under the
vm exclusive and stays.

### Agent account removal

`delete_agent_on_vm` (`agents/initializer.py:390-425`, reached from agent delete at
`agents/manager/lifecycle.py:435` and from agent create's rollback at `agents/realize.py:108`) runs
`userdel -r`, which keeps shadow's passwd and group locks while it removes the home and the mail
spool. A long home removal would then stall every other account edit on the VM past `lckpwdf`'s 15
s, and they would fail. So it splits, keeping its `pkill` and socket-directory removal first and its
warn-on-step-failure handling:

1. Probe, as root: the account's uid and home from `getent passwd` (`passwd_lookup`,
   `agents/initializer.py:44`), and the spool directory from `MAIL_DIR` in `/etc/login.defs`
   (Debian's default `/var/mail`).
2. `userdel <user>`. Without `-r` it still removes the passwd, shadow and group entries and the
   user-private group (`USERGROUPS_ENAB`); `-r` adds only the home and the spool.
3. One root script removes the home with `rm -rf --one-file-system --` only when it is a directory
   still owned by the probed uid, and `<MAIL_DIR>/<user>` only when it is a regular file owned by
   that uid, keeping `userdel`'s rule of not removing what the user does not own and never following
   into another file system. It runs under the account's exclusive, held by agent delete and by the
   creating operation on rollback, so no create of the same account can start while the freed name
   still has a home.

A failure in step 3 warns through the existing `STEP_FAILURES` handler, as a `userdel -r` exit 12
(home or spool removal failed) does today.

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

New tests live beside the package (`tests/native_locks/`); every primitive test is marked
`pytest.mark.windows` so the Windows job (`.github/workflows/ci.yml:97-115`) runs it. Assertions
read the error's fields (`entity_kind`, `entity_name`, mode, `holder.command`, `holder.held_for`),
never its wording.

| Test                  | Proves                                                                                                                                                                                                                                           |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| conflict matrix       | the four mode pairs on one entity, within a process (two sets) and across processes (a subprocess holder), on both platforms                                                                                                                     |
| tree                  | a session change refuses a workspace change and the vm exclusive, and is refused by them; sessions in different workspaces and accounts coexist; workspace delete and agent delete on a shared session refuse one another                        |
| ordering              | two contending acquisitions never both fail                                                                                                                                                                                                      |
| probe                 | a conflicting probe raises naming the holder and writes no record; a free probe leaves nothing held                                                                                                                                              |
| crashed holder        | a killed subprocess holder frees every entity (bounded poll on Windows) and its record is removed by the next reader                                                                                                                             |
| old-version interlock | a subprocess running today's algorithm inline (`flock` exclusive, or `msvcrt.locking` after `seek(0)`) refuses new shared and exclusive acquisitions with the fallback, and a new shared holder makes the old algorithm fail with its busy errno |
| holder naming         | the earliest conflicting live holder is named with its command, entity and age; a stale record is skipped and deleted; an malformed one falls back; non-conflicting records are ignored                                                          |
| operation label       | a command invoked through the CLI runner with a distinctive argument value records its command path and no part of that value; outside the CLI the label is `agw`                                                                                |
| nested reuse          | `require` acquires nothing; an uncovered or weaker entity, a different VM or database, and a released set each raise `StateError`; a console whose members changed still matches by key                                                          |
| prompt notice         | a prompt helper or resolver prompt raised under a set that has not announced fails with `StateError`; after `announce_prompt` it prompts                                                                                                         |
| activation            | a cold VM boots under a transient vm exclusive and the body holds the vm shared; `auto_start` under a shared set refuses; step 6 refuses a changed vm_uuid, intent or host; a cold family's intent survives a failed boot                        |
| account removal       | against a scripted guest: plain `userdel`, then the home and spool removed only when owned by the probed uid; a home owned by someone else is kept                                                                                               |
| ensure_group          | two concurrent workspace creates whose group already exists both succeed                                                                                                                                                                         |
| rebuild lock          | a second SSH config rebuild waits for the first and then reads the newer database state; a lock held past the bound raises the typed error                                                                                                       |
| grant-all             | a workspace create and a grant-all agent create on one VM: one is refused before native work, and after a retry every grant-all agent has a grant for every workspace                                                                            |
| rollback              | a failing `session create --new-workspace --new-agent` unwinds without a refusal while unrelated holders exist on the VM                                                                                                                         |
| rehome re-check       | a session started between rehome's confirmation and its acquisition refuses the rehome                                                                                                                                                           |

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

### Implementation-time check: harness setup confinement

The lock sets assume `apply_workspace_setup` and `apply_agent_setup` write only inside their owner
(the workspace tree, the account home); `apply_vm_setup` runs under the vm exclusive and is covered
whatever it writes. The implementation PR confirms this for every harness integration's writers, for
each one either with a test that a setup applied for one owner leaves another owner's tree
untouched, or with a review cited in the PR naming the writer and the paths it can reach. Only the
cleanup paths (`harness_setup/cleanup.py:25-42`) were checked for this LLD. A writer that crosses
owners adds the other owner to its request, exclusive, before the PR merges.

### Existing tests that change

| File                                                                                                                                                                                                                                                                                                                                                    | Why                                                                                                                   |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `tests/capabilities/test_native_setup_locking.py`                                                                                                                                                                                                                                                                                                       | replaced by `tests/native_locks/`                                                                                     |
| `tests/vms/test_vm_nodes.py` (`:436-511`)                                                                                                                                                                                                                                                                                                               | `auto_start` and admission tests move to the sequence of section 2                                                    |
| `tests/sessions/test_console_preparation.py` (`:139`, `:363`)                                                                                                                                                                                                                                                                                           | delete consent now precedes the lock; the monkeypatched guard target moves                                            |
| `tests/sessions/test_session_spec_launch.py`, `tests/vms/test_rekey_key_selection.py`, `tests/vms/test_recover.py`                                                                                                                                                                                                                                      | consent and auth-key prompts move before acquisition; recover's confirm stays inside                                  |
| `tests/runtime_state/test_failures.py`, `test_composition.py`, `test_preview_trust.py`                                                                                                                                                                                                                                                                  | per-family probe and busy reporting, held-set reuse, dry run still takes no lock                                      |
| `tests/test_session_ephemeral_guard_unwind.py`, `tests/sessions/test_create_start_restart_orchestrated.py`, `tests/agents/test_create_reinit_orchestrated.py`, `tests/vms/test_lifecycle_orchestrated.py`                                                                                                                                               | held set instead of the guard token; acquisition after the gate                                                       |
| `tests/capabilities/test_native_setup_lifecycle.py`, `test_native_setup_deletion.py`, `test_native_setup_state.py`                                                                                                                                                                                                                                      | `held=` becomes `held.require` with the owner's request                                                               |
| `tests/test_consoles_orchestration.py` (`:704`)                                                                                                                                                                                                                                                                                                         | `delete_console_record` (`sessions/multi_console/crud.py:609`), dead outside this test, is deleted rather than ported |
| `tests/sessions/test_prepared_runtime.py`, `test_console_value_barrier.py`, `test_console_attach_orchestrated.py`, `tests/artifacts/test_session_lifecycle.py`, `tests/test_wsl2_keepalive.py`, `tests/vms/test_reinit_admin_options.py`, `tests/vms/test_delete_vm_gating.py`, `tests/test_legacy_claude.py`, `tests/orchestration/test_activation.py` | construct or patch the old guard; mechanical port                                                                     |

## 7. Collateral for the implementation PR

Every item states the current single-guard behavior and goes stale with this change; the HLA's
delivery list names the same set.

- `cli/command-reference.md:687-692`, the busy paragraph: hierarchical sets, what refuses what, the
  holder message, prompts before locks with their three exceptions and the notice line, admission
  still not spanning a body.
- `cli/agentworks/orchestration/README.md:166-170`: held sets replace the family guard and token;
  the operation sequence.
- `docs/guides/upgrading-to-0.20.md`: operations below the VM now run concurrently, a refusal names
  its holder, and an older agw still running during the upgrade interlocks safely but reports busy
  without naming holders.
- `docs/guides/desired-runtime-state.md:154`, `:161-165`.
- `docs/guides/native-harness-setup.md:336-340`, `docs/guides/harness-facets.md:400`,
  `docs/guides/idempotency.md:298`, `cli/agentworks/db/README.md:138`,
  `cli/agentworks/capabilities/harness_integration/README.md:376`: "VM-family mutation guard"
  becomes the owner's lock.
- `cli/agentworks/guide/guide-content/troubleshooting.md:55-77`, a guide topic: busy causes and the
  holder message.
- The `native_locks` package docstring carries the durable rules (tree, modes, order, no upgrade,
  read and lock once, records advisory) so they outlive this SDD.

No sample-config or completion change: the waits are constants and the command tree is unchanged.

## Appendix A: implementation checklist

Each row is a current acquisition site to move into the sequence of section 2. "Prompts under it"
lists what step 3 takes out of the lock.

| Command                                            | Acquires today                                                                                                                                                                                                                            | Prompts under it today                                                                                 |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `vm create`                                        | `vms/manager/lifecycle.py:383` (by name)                                                                                                                                                                                                  | none (site, slug and secrets come first)                                                               |
| `vm reinit`                                        | `vms/manager/reinit.py:71`, before every read                                                                                                                                                                                             | gate and boundary secrets (`reinit.py:315`, `:331`)                                                    |
| `vm start`, `stop`                                 | `vms/manager/power.py:73`, `:117`                                                                                                                                                                                                         | gate secrets (`vms/manager/runtime.py:73`)                                                             |
| `vm delete`                                        | `power.py:218`, after its confirm (`:213`)                                                                                                                                                                                                | boundary secrets (`boundary.py:289`)                                                                   |
| `vm rekey`                                         | `power.py:412`                                                                                                                                                                                                                            | auth key and `--prompt` (`boundary.py:346`, `:361`); `--wait-for-share` stays (`:490`)                 |
| `vm recover`                                       | `vms/manager/recover.py:84`                                                                                                                                                                                                               | secrets (`boundary.py:361`); host-key confirm stays (`recover.py:240`)                                 |
| `vm set-desired-state`                             | `power.py:36`                                                                                                                                                                                                                             | none                                                                                                   |
| `vm reconcile`, session batch, `console reconcile` | `runtime_state/_selection.py:101`, families in VM name order                                                                                                                                                                              | gate secrets (`runtime_state/_prepare.py:213-221`); workload union stays (`_execute.py:45`)            |
| `vm import-host-keys`                              | none (`vms/manager/host_keys.py:126-149`)                                                                                                                                                                                                 | none                                                                                                   |
| `vm confirm-release`                               | none (`vms/manager/release.py:67`, writes `:86-89`)                                                                                                                                                                                       | confirm after the boundary (`:79`)                                                                     |
| access commands                                    | admission only: `vms/manager/exec.py:121`, `:193`, `:278`; `port_forward.py:80`; `vms/backup.py:81`; `agents/manager/access.py:143`, `:244`; `sessions/manager/_queries.py:938`; `_logs.py:44`; `sessions/multi_console/lifecycle.py:257` | gate secrets come first already                                                                        |
| `agent create`                                     | `agents/realize.py:119`, after the gate (`agents/manager/lifecycle.py:192`)                                                                                                                                                               | none                                                                                                   |
| `agent reinit`                                     | `agents/manager/lifecycle.py:504`, before the gate (`:646`)                                                                                                                                                                               | gate and boundary secrets (`:658`, `:666`)                                                             |
| `agent delete`                                     | `lifecycle.py:331`, after its confirm (`:324`), before the boundary (`:347`)                                                                                                                                                              | boundary secrets; the session list it uses is read before the guard (`:307`)                           |
| `agent grant-workspaces`, `revoke`                 | none (`agents/grants.py:95`, `:162`)                                                                                                                                                                                                      | none                                                                                                   |
| `workspace create`                                 | `workspaces/realize.py:124`, after the gate (`workspaces/manager/create.py:283`)                                                                                                                                                          | none                                                                                                   |
| `workspace delete`                                 | `workspaces/manager/delete.py:106`, after its confirm (`:101`)                                                                                                                                                                            | boundary secrets (`:134-142`)                                                                          |
| `workspace repair`                                 | none (`workspaces/manager/repair.py:132`)                                                                                                                                                                                                 | none                                                                                                   |
| `workspace rehome`                                 | `workspaces/manager/rehome.py:96`                                                                                                                                                                                                         | confirm (`:217`), secrets (`:181-189`)                                                                 |
| `workspace copy`                                   | none (`workspaces/manager/copy.py:107`, `:144`)                                                                                                                                                                                           | none (destination choice `:140` is unlocked)                                                           |
| `session create`                                   | `sessions/manager/_create.py:377`, before the gate (`:414`)                                                                                                                                                                               | gate and boundary secrets (`:414-417`)                                                                 |
| `session start --spec`                             | `sessions/manager/_spec.py:128`                                                                                                                                                                                                           | restart consent (`:135`)                                                                               |
| `session set-desired-state`                        | `sessions/manager/_intent.py:59`                                                                                                                                                                                                          | none                                                                                                   |
| `session delete`                                   | `sessions/manager/_queries.py:154`, before the boundary (`sessions/manager/_scope.py:114`)                                                                                                                                                | confirm (`_queries.py:227`), empty-console offer (`:295`, `sessions/_resource_cleanup.py:97`), secrets |
| `console create`                                   | `sessions/multi_console/crud.py:163`                                                                                                                                                                                                      | gate and pane secrets (`attach.py:453`, `:495`)                                                        |
| `console start`, `restart`, `stop`                 | `multi_console/lifecycle.py:69`, `:149`                                                                                                                                                                                                   | gate secrets (`:96`, `:167`); pane secrets stay (`:117-118`, `:191`)                                   |
| `console delete`                                   | `multi_console/attach.py:854`                                                                                                                                                                                                             | confirm (`:856`)                                                                                       |
| `console set-desired-state`                        | `multi_console/runtime.py:132`                                                                                                                                                                                                            | none                                                                                                   |
| `console add-sessions`, `add-shell`                | `crud.py:325`, `:648`                                                                                                                                                                                                                     | pane secrets stay (`:379`, `:696`)                                                                     |
| `console remove-sessions`, `reorder-sessions`      | `crud.py:466`, `:525`                                                                                                                                                                                                                     | empty-console offer (`:494`)                                                                           |
| `console restore-session`                          | `multi_console/restore.py:293`                                                                                                                                                                                                            | gate secrets; pane secrets stay (`:353`)                                                               |
