# Native lock hierarchy: low-level design

<!-- cspell:ignore contextmanager ctypes wintypes inodes -->

Status: draft. Builds on the accepted [FRD](./frd.md) and the draft [HLA](./hla.md). Code citations
are `file:line` under `cli/agentworks/` at 27b215e9b unless a path says otherwise.

This document fixes the primitive, the holder records, the lock set of every command, the mechanism
for each piece of shared state, the tests and the collateral. [Decisions](#7-decisions) records the
lead's rulings on the questions the first draft left open, and
[Remaining differences from the HLA](#remaining-differences-from-the-hla) lists where this LLD and
the HLA still disagree.

## 1. The primitive

### Package

A new package `native_locks/` replaces `harness_setup/locking.py` (`native_mutation_guard`,
`NativeMutationGuard`, `NativeSetupBusyError`) in one step. The lock serves every native mutation,
not harness setup, so it moves out of `harness_setup` and its names stop saying "setup".

| Module         | Holds                                                                                                     |
| -------------- | --------------------------------------------------------------------------------------------------------- |
| `__init__.py`  | `LockMode`, `EntityKind`, `Entity`, `LockRequest`, `NativeLockSet`, `native_locks`, `NativeLockBusyError` |
| `_entities.py` | entity constructors and ancestor closure                                                                  |
| `_os.py`       | the platform calls: open, try-lock shared or exclusive, unlock                                            |
| `_records.py`  | holder records and the holder lookup a refusal uses                                                       |
| `rows.py`      | entity builders from database rows (`session_entity_for(db, row)` and the like)                           |
| `mutex.py`     | the bounded-wait workstation mutex of [section 4](#ssh-config-and-other-workstation-rebuilds)             |

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

| Constructor                                  | Display key                   | Notes                                                    |
| -------------------------------------------- | ----------------------------- | -------------------------------------------------------- |
| `vm_entity(vm)`                              | `vm/<vm>`                     | the existing vm file                                     |
| `workspace_entity(vm, name)`                 | `workspace/<name>`            |                                                          |
| `account_entity(vm, linux_user)`             | `account/<linux user>`        | `AgentRow.linux_user` or `VMRow.admin_username`          |
| `grant_all_entity(vm)`                       | `grant-all`                   | the VM's set of grant-all agents (added here, see below) |
| `grant_entity(workspace, account)`           | `grant/<workspace>+<account>` | one membership of one account in one workspace group     |
| `session_entity(name, workspace=, account=)` | `session/<name>`              |                                                          |
| `console_entity(vm, name, members=)`         | `console/<name>`              | members are session entities                             |

Accounts are keyed by Linux user rather than agent name because the account is the thing on the VM
(`agents/manager/_common.py:109` derives `agt-<name>` at create; legacy rows keep their stored
user), and the admin and every agent are distinct users on one VM, so no key collides.

The `grant-all` entity names a set no other entity owns. A grant-all agent must hold a grant for
every workspace on its VM (`agents/grants.py:296`). Workspace create reads the set of grant-all
agents (`workspaces/realize.py:209`), and agent create with `--grant-all-workspaces`, grant `--all`
and revoke `--all` change that set and then read the set of workspaces (`agents/realize.py:225-230`,
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

    def modes(self) -> dict[Entity, LockMode]: ...
```

`modes()` gives every entity in `exclusive` the exclusive mode and every other entity in `shared` or
in the ancestor closure of either set the shared mode. All entities of one request name one VM, and
the constructor refuses otherwise; a command that spans VMs holds one set per VM. A request with no
exclusive entity and only the vm shared is the admission request of an access command. The same
entity appearing twice with different parents is refused, since it means two reads disagreed.

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

### Acquire, extend, release

```python
@contextmanager
def native_locks(
    database_path: Path, request: LockRequest, *, held: NativeLockSet | None = None
) -> Iterator[NativeLockSet]: ...


class NativeLockSet:
    database_path: Path
    vm: str

    def covers(self, entity: Entity, mode: LockMode) -> bool: ...
    def require(self, request: LockRequest) -> None: ...
    def extend(self, request: LockRequest) -> None: ...
```

- **Acquire** (`held=None`): open each file of `request.modes()` in order and try its mode without
  waiting. The first refusal releases everything this call took, then raises `NativeLockBusyError`.
  On success the set writes its holder record ([section 2](#2-holder-records)) and is yielded; on
  exit it releases every entity, then removes the record.
- **Extend**: acquire the entities of a request the set does not already cover, in order. A refusal
  releases only what this call took and raises; everything held before stays held, and the caller
  unwinds as on any other error. Extended entities stay held until the set itself is released. An
  entity held shared and requested exclusive raises `StateError` (an interior contract, not a
  refusal): neither `flock` conversion (documented as non-atomic, `flock(2)`) nor `LockFileEx`
  (which cannot lock exclusive over its own shared range) upgrades safely, so callers request the
  strongest mode they will need up front. Operations extend as soon as they have read what they
  depend on and before their first native change, so an extension refusal never strands half-done
  native work.
- **Nested reuse** (`held=` given): replaces today's `held=`. The nested operation states the
  request for what it changes; the call checks the set is active and for the same database and VM
  (as `harness_setup/locking.py:46-48` does today), then extends it, which takes nothing when the
  set already covers the request. A nested operation never opens a second handle on an entity its
  caller holds: within one process two handles conflict on both platforms, which is why reuse
  exists.
- **Require**: assert coverage without acquiring, for code that must not lock on its own (the boot
  and power paths of [Activation](#activation-under-the-hierarchy)). Missing coverage raises
  `StateError`.
- Set mutation is serialized by an internal `threading.Lock`, because the teardown coordinator
  submits work to threads that share the caller's set (`orchestration/README.md:188-193`). Only the
  owning thread extends or releases.

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
processes lock different inodes. The cost is one one-byte file per entity name ever locked. Not even
`vm delete` removes its `<sha256(vm)>.d/` directory, since another process may still hold one of its
files open.

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
above and is proved by the Windows-marked interlock test in [section 5](#5-tests), which must pass
before merge. Old and new processes therefore exclude each other correctly during an upgrade, with
the old one coarser.

### Crash release

The OS releases every lock when the holder exits or is killed: `flock` locks belong to the open file
description, closed at exit; Windows releases a terminated process's byte-range locks (possibly
after a short delay, which the tests tolerate by polling for a bounded few seconds). Lock files are
opened non-inheritable (Python's default, PEP 446) and agw starts subprocesses with `close_fds`, so
a surviving child such as an `ssh` process cannot keep a lock alive; the implementation must not
pass these descriptors to children or `fork` without `exec`. A crashed holder leaves its holder
record behind; [section 2](#stale-records) removes it.

### Why nothing deadlocks

No acquisition of the native lock waits: every call fails immediately. A process that waits for
nothing cannot be part of a wait cycle. The only bounded waits in the design (the guest accounts
lock and the workstation rebuild mutex of [section 4](#4-shared-state-across-entities)) are leaf
critical sections: while inside one, a process acquires nothing else and runs one short command or
one file rebuild, so they cannot form a cycle either, and each has a deadline.

The fixed order adds a liveness property: two single-pass acquisitions that contend cannot both be
refused. If A is refused at entity `a` (held by B) and B at entity `b` (held by A), then A holds `b`
and was refused at `a`, so `b < a`; symmetrically `a < b`, a contradiction. Extensions acquire out
of that order (an agent delete extends with workspaces after holding its account), so two extending
operations can both be refused; the refusal is safe and each retries.

### Activation under the hierarchy

A stopped VM is booted by `LiveVMNode.auto_start` (`vms/nodes.py:236-271`) and by
`activate_vm_runtime` (runtime path), both under the vm lock today. Booting is a VM-wide change, so
it needs the vm exclusive, which an operation holding the vm shared cannot get by upgrade. Commands
fall in two patterns:

- **Gate first.** Commands composed through `gated_vm_boundary` (`vms/manager/boundary.py:192`) or
  `activation_gate` (`orchestration/activation.py:158`) run the gate and the boundary resolve before
  acquiring their own set. With no set passed, `auto_start` takes a transient `{vm: exclusive}` for
  the boot and `hold_active` a transient `{vm: shared}` for its admission re-read
  (`nodes.py:273-285`), each released before the next step, exactly the shape `hold_active` has
  today. The operation then acquires its set and calls `vm_node.require_admitted(held)`: under the
  held set it re-reads the row and refuses with `StateConflictError` if the vm_uuid, the desired
  runtime state or the Tailscale host changed since admission (a `vm stop`, `delete`, `rekey` or
  `recover` that ran in between). A VM-wide command passes its exclusive set into the gate instead,
  and `auto_start` reuses it.
- **Runtime first.** Commands that must record intent before activation (`runtime_state` families,
  console start, restart, stop, create, restore and attach, `session start --spec`) acquire first.
  They pick the vm mode just before acquiring with `activation_vm_mode(node)`: exclusive when
  `node.confirmed_active()` is false (the gate's own fast path, `nodes.py:196-208`; a row with
  stopped intent counts as not confirmed), shared otherwise. Power actions call
  `held.require({vm: exclusive})`; under a shared set a VM found stopped (it stopped between the
  probe and the acquisition) refuses with
  `StateError("VM '<name>' stopped after this command checked it", hint="Run the command again.")`
  rather than booting.

Admission (`hold_active`) never holds the native lock across the body of any command. An access
command's admission set is released before its interactive body, as today (`nodes.py:285`,
`cli/command-reference.md:690-691`). Holding the vm shared across an interactive body would refuse
`vm stop`, `reinit`, `rekey`, `recover` and `delete` for as long as any shell, attach, `logs -f` or
port forward stays open, a refusal by an operation that changes nothing.

## 2. Holder records

### Format and location

One JSON file per held set, `<sha256(vm)>.d/holders/<pid>-<16 hex>.json`, mode 0600:

```json
{
  "version": 1,
  "pid": 4242,
  "command": ["agw", "workspace", "rehome"],
  "acquired": 1795400000.25,
  "entities": [
    { "key": "vm/dev", "mode": "shared", "since": 1795400000.25 },
    { "key": "workspace/api", "mode": "exclusive", "since": 1795400000.25 }
  ]
}
```

`command` is the Click command path of the running command
(`click.get_current_context(silent=True).command_path`), its first word normalized to `agw`; outside
a Click context it is `["agw"]`. The command path is the chain of command names Click parsed, which
contains no argument or option values. `since` is wall-clock acquisition time per entity, so an
extension's entities report their own age.

### Write, update, remove

- After the set's first acquisition succeeds, the holder creates the file with `O_CREAT | O_EXCL`,
  takes an exclusive lock on it (POSIX `flock`; Windows `LockFileEx` exclusive at offset `1 << 62`,
  past the content, so readers can still read it), and writes the JSON. The open handle and its lock
  live as long as the set.
- After each successful extension the holder rewrites the content in place (truncate, write).
- On release the holder releases every entity lock, then unlocks, closes and unlinks the record.

### Stale records

A record is live exactly while its own lock is held, which the OS releases when the holder dies. A
reader tests liveness by trying a shared lock on the record without waiting (same offset on
Windows): success on a record that parses means the holder is gone, so the reader unlocks and
unlinks it (ignoring a failed unlink, which means another reader got there first). No pid probing is
needed, so pid reuse cannot make a dead holder look alive. A record that does not parse counts as
unknown and is neither named nor deleted: locked, a reader raced a rewrite; unlocked, a holder may
be between creating the file and locking it, so it is deleted only once its modification time is
more than a minute old (a holder that died before writing).

### Naming the holder

On a refusal at entity `e` in mode `m`, the refused process lists the VM's `holders/`, drops stale
and unknown records, keeps those listing `e` in a mode that conflicts with `m`, and names the one
whose `since` for `e` is oldest, adding the count of the others. The record is advisory: the OS lock
already decided, so a missing or unreadable record only falls back to the generic message.

The R3 error and hint, with `<subject>` the first exclusive entity of the refused request (or
`vm/<name>` for an admission request):

```text
Error: session/build-42 on VM 'dev' is blocked: `agw workspace rehome` has held workspace/api for 3m12s
  Hint: Retry when that operation finishes.
Error: workspace/api on VM 'dev' is blocked: `agw session start` has held it for 41s (and 2 other operations)
  Hint: Retry when that operation finishes.
Error: session/build-42 on VM 'dev' is blocked: another operation holds workspace/api
  Hint: Retry when that operation finishes.
```

Durations print as `41s`, `3m12s` or `1h05m`. The message says "is blocked" rather than naming the
refused verb: the primitive does not know the verb, and the subject plus the holder already tell the
operator what to wait for.

### What a record never contains

Argument or option values, secret names or values, environment, paths, hostnames, prompt answers, or
anything else read from the operator. Entity names are identifiers already in the database beside
it, under the same 0700 directory and the same OS user. The only fields are the ones above.

## 3. Lock sets per command

### Conventions

- **X** lists the entities taken exclusive; every ancestor of an X entity is taken shared without
  being listed. **S** lists extra shared entities. A console's ancestors are the vm and its member
  sessions with their workspaces and accounts.
- **vm\*** means the vm in `activation_vm_mode` (runtime-first pattern).
- **Extend** lists what the operation adds after reading state, before native work.
- "Today" cites where the current guard is taken; "New" is where the set is acquired.
- Every command that resolves secrets resolves them before its set unless the prompt table below
  says otherwise. Dry runs take no lock, as today.
- `harness_setup/dispatch.py:116` (`run_setup`) and `harness_setup/lifecycle.py:262`, `:312`, `:365`
  (`apply_vm_setup`, `apply_agent_setup`, `apply_workspace_setup`) become nested requests for vm X,
  the account X and the workspace X respectively, always covered by their callers.

### VM commands

| Command                | X / S                               | Today                                        | New                                                                      | Nested reuse                                                               |
| ---------------------- | ----------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------ | -------------------------------------------------------------------------- |
| `vm create`            | X vm (by name, before the row)      | `vms/manager/lifecycle.py:383`               | same place                                                               | `run_initialization` (`vms/initializer/driver.py:187`), setup dispatch     |
| `vm reinit`            | X vm                                | `vms/manager/reinit.py:71`, before all reads | after the pre-read row's boundary resolve; reload, compare vm_uuid       | gate (`reinit.py:197`, `:315`), `run_initialization` (`:397`)              |
| `vm start`             | X vm                                | `vms/manager/power.py:73`                    | after gate secrets resolve; existing reload (`:77`)                      | `prepare_vm_runtime` (`:90`), `hold_active` (`vms/manager/runtime.py:143`) |
| `vm stop`              | X vm                                | `power.py:117`                               | after gate secrets resolve; existing reload (`:121`)                     | `prepare_vm_runtime` (`:133`)                                              |
| `vm delete`            | X vm                                | `power.py:218`, after confirm `:213`         | after confirm and `_live_vm_boundary` resolve; reload uuid (`:219-221`)  | `hold_active` (`:260`)                                                     |
| `vm rekey`             | X vm                                | `power.py:412`                               | after `_auth_key_boundary` resolve; existing checks (`:413-421`)         | gate (`:457`) reuses                                                       |
| `vm recover`           | X vm                                | `vms/manager/recover.py:84`                  | after `_auth_key_boundary` resolve; existing checks (`:85-90`)           | `hold_active` (`:109`)                                                     |
| `vm set-desired-state` | X vm                                | `power.py:36`                                | same                                                                     |                                                                            |
| `vm reconcile`         | per family X vm                     | `runtime_state/_selection.py:101`            | same, after per-family gate secrets ([below](#runtime_state-families))   | `prepare_vm_runtime`, `accept_session_runtime_state`, console prepare      |
| `vm import-host-keys`  | per VM X vm, around that VM's write | none (`vms/manager/host_keys.py:126-149`)    | in `_import_one`; a refusal is a new per-VM `BUSY` outcome, others go on |                                                                            |
| `vm confirm-release`   | X vm                                | none (`vms/manager/release.py:67`, `:86-89`) | after the confirm (`:79`); reload uuid and the observed release          |                                                                            |

### Access and observe commands

These take the admission request (vm shared, exclusive only for a boot) through the gate and release
it before the body.

| Command                                                                                                                                | Today (admission only)                                                   | New                             |
| -------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------- |
| `vm shell`, `vm shell --platform`                                                                                                      | `vms/manager/exec.py:121`, `:193`                                        | transient admission, gate first |
| `vm exec`                                                                                                                              | `exec.py:278`                                                            | same                            |
| `vm port-forward`                                                                                                                      | `vms/manager/port_forward.py:80`                                         | same                            |
| `vm backup`                                                                                                                            | `vms/backup.py:81`                                                       | same                            |
| `agent shell`, `agent exec`                                                                                                            | `agents/manager/access.py:143`, `:244`                                   | same                            |
| `session attach`, `session logs`                                                                                                       | `sessions/manager/_queries.py:938`, `sessions/manager/_logs.py:44`       | same                            |
| `console attach`                                                                                                                       | `sessions/multi_console/lifecycle.py:257`, released `:303` before `:304` | vm\* admission, runtime first   |
| `vm describe`, `vm verify-connection`, `vm logs`, `session describe`, `session list`, `console describe`, `console list`, every `list` | none                                                                     | none (unchanged)                |

### Agent and workspace commands

| Command                   | X / S                                                                                              | Today                                                                        | New                                                                                | Extend                                                                                          |
| ------------------------- | -------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `agent create`            | X account (derived user); X grant-all with `--grant-all-workspaces`                                | `agents/realize.py:119`, after gate (`agents/manager/lifecycle.py:192`)      | same place                                                                         | with grant-all: X grant per workspace on the VM (`realize.py:228`)                              |
| `agent reinit`            | X account                                                                                          | `agents/manager/lifecycle.py:504`, before gate `:646`                        | gate first; re-read agent row (vm_name, linux_user)                                | X each recorded grant it replays (`:783`)                                                       |
| `agent delete`            | X account; X grant-all when the agent is grant-all                                                 | `lifecycle.py:331`, after confirm `:324`, before boundary `:347`             | gate first; re-read the agent, its sessions, grants and consoles                   | X each session (`:307`), each grant (`:431`), each console holding one of its sessions (`:425`) |
| `agent grant-workspaces`  | X grant per named workspace; `--all` adds X grant-all                                              | none (`agents/grants.py:95`)                                                 | gate first, then acquire                                                           | `--all`: X grant per workspace listed under the set                                             |
| `agent revoke-workspaces` | X grant per named workspace; `--all` adds X grant-all                                              | none (`grants.py:162`)                                                       | gate first, then acquire                                                           | `--all`: X grant per granted workspace (`grants.py:180`)                                        |
| `workspace create`        | X workspace (by name), X grant(workspace, admin); S grant-all                                      | `workspaces/realize.py:124`, after gate (`workspaces/manager/create.py:283`) | same place                                                                         | X grant(workspace, agent) per grant-all agent, read under the shared set (`realize.py:209`)     |
| `workspace delete`        | X workspace                                                                                        | `workspaces/manager/delete.py:106`, after confirm `:101`                     | gate first; re-read sessions and compare with the confirm                          | X each session (`:124`), each grant (`grants.py:217`), each console holding one (`:184`)        |
| `workspace repair`        | X workspace, X grant(workspace, admin)                                                             | none (`workspaces/manager/repair.py:132`)                                    | gate first, then acquire                                                           | X grant per agent it adds or removes (`repair.py:322`, `:329`)                                  |
| `workspace rehome`        | X workspace                                                                                        | `workspaces/manager/rehome.py:96`                                            | gate and confirm first; re-read row path, re-run the probes (`:191-206`)           | none                                                                                            |
| `workspace copy`          | source VM: S workspace(source); destination VM: X workspace(new), X grant(new, admin), S grant-all | none (`workspaces/manager/copy.py:107`, `:144`)                              | both gates first; one set per VM in VM name order; one set if the VMs are the same | destination: X grant per grant-all agent (`copy.py:275`)                                        |

### Session and console commands

| Command                                 | X / S                                                        | Today                                                 | New                                                                            | Nested reuse                                                                                         |
| --------------------------------------- | ------------------------------------------------------------ | ----------------------------------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| `session create`                        | X session (by name), X grant(workspace, agent) in agent mode | `sessions/manager/_create.py:377`, before gate `:414` | gate and resolve (`:417`) first; then acquire; existing VM reload (`:378-386`) | `realize_workspace` (`_create_roll.py:87`), `realize_agent` (`:123`), session slice (`:248`)         |
| `session create --new-workspace`        | adds X workspace, X grant(workspace, admin), S grant-all     | as above                                              | as above                                                                       | `realize_workspace` extends with grant-all grants                                                    |
| `session create --new-agent`            | adds X account                                               | as above                                              | as above                                                                       | `realize_agent`                                                                                      |
| `session start`, `restart`, `stop`      | per family X session; vm\*                                   | `runtime_state/_selection.py:101`                     | same, after per-family gate secrets                                            | intent (`sessions/manager/_intent.py:37`), launch                                                    |
| `session start --spec`                  | X session; vm\*                                              | `sessions/manager/_spec.py:128`                       | after consent (moved); same place                                              | `run_session_batch(held=)` (`_spec.py:149`)                                                          |
| `session reconcile`                     | per family X session; vm\*                                   | `_selection.py:101`                                   | same                                                                           |                                                                                                      |
| `session set-desired-state`             | X session                                                    | `_intent.py:59`                                       | same                                                                           | `accept_session_runtime_state` (`_intent.py:60`)                                                     |
| `session delete`                        | X session, X each console holding it                         | `_queries.py:154`, before gate `_scope.py:114`        | confirm and console offer first; gate; acquire; re-read session uuid (`:155`)  | `offer_delete_if_empty_consoles` (`:295`) to `delete_console(held=)` (`multi_console/attach.py:930`) |
| `console create`                        | X console (by name); vm\*                                    | `multi_console/crud.py:163`                           | same                                                                           | `_active_console_vm` (`crud.py:231`)                                                                 |
| `console start`, `restart`              | X console; vm\*                                              | `multi_console/lifecycle.py:69`                       | same                                                                           | `accept_console_running` (`runtime.py:143`), reconcile (`:177`, `:270`)                              |
| `console stop`                          | X console; vm\*                                              | `lifecycle.py:149`                                    | same                                                                           | `prepare_console_stop` (`runtime.py:221`), `execute_console_stop` (`:304`)                           |
| `console reconcile`                     | per family X console; vm\*                                   | `_selection.py:101`                                   | same                                                                           |                                                                                                      |
| `console set-desired-state`             | X console                                                    | `runtime.py:132`                                      | same                                                                           |                                                                                                      |
| `console delete`                        | X console                                                    | `attach.py:854`, confirm under it `:856`              | confirm first; acquire; re-read console (`:855`)                               | from session delete and `remove-sessions` with `held=`                                               |
| `console add-sessions`                  | X console; S each added session                              | `crud.py:325`                                         | same                                                                           |                                                                                                      |
| `console remove-sessions`               | X console                                                    | `crud.py:466`                                         | empty-console offer first; same place                                          | `offer_delete_if_empty_consoles` (`crud.py:494`)                                                     |
| `console reorder-sessions`, `add-shell` | X console                                                    | `crud.py:525`, `:648`                                 | same                                                                           |                                                                                                      |
| `console restore-session`               | X console; vm\*                                              | `multi_console/restore.py:293`                        | same                                                                           | `_active_console_vm` (`restore.py:315`)                                                              |

`guarded_console_selection` (`multi_console/_helpers.py:221-240`) becomes the console request
builder: it pre-reads the console and its membership, acquires `X console` (whose entity carries the
members), then re-reads and compares the VM and the membership, refusing with `StateConflictError`
if either changed.

### Prompts (R6)

| Prompt                                                                                                 | Today                                                                                                                                     | New                                                                                                                                                                                                                                                                                                      |
| ------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Boundary and gate secrets, every gate-first command                                                    | inside the guard where it precedes the gate (`boundary.py:180`, `:187`)                                                                   | before acquisition; the operation re-reads what it acts on after acquiring                                                                                                                                                                                                                               |
| `vm start`, `stop` gate secrets                                                                        | `vms/manager/runtime.py:73`, under `power.py:73`, `:117`                                                                                  | resolved first; `prepare_vm_runtime` serves the cache (`resolve_gate`)                                                                                                                                                                                                                                   |
| `vm reinit`, `vm delete` secrets                                                                       | under `reinit.py:71`, `power.py:218`                                                                                                      | before acquisition; reload and compare vm_uuid                                                                                                                                                                                                                                                           |
| `vm rekey` auth key and `--prompt`                                                                     | under `power.py:412` (`boundary.py:346`, `:361`)                                                                                          | before acquisition; existing checks after                                                                                                                                                                                                                                                                |
| `vm rekey --wait-for-share` pause                                                                      | `power.py:490`                                                                                                                            | stays inside, after the notice line: it pauses the rekey itself; holds only the vm, exclusive anyway                                                                                                                                                                                                     |
| `vm recover` host-key confirm                                                                          | `recover.py:240`                                                                                                                          | stays inside, after the notice line: it confirms a key the locked recovery just observed; holds only the vm                                                                                                                                                                                              |
| `vm delete` confirm                                                                                    | before, `power.py:213`                                                                                                                    | unchanged                                                                                                                                                                                                                                                                                                |
| `vm confirm-release` confirm                                                                           | unlocked, `release.py:79`                                                                                                                 | before acquisition; reload the row and the observed release after                                                                                                                                                                                                                                        |
| `agent delete`, `workspace delete` confirm                                                             | before, `lifecycle.py:324`, `delete.py:101`                                                                                               | before; after acquiring, the session set must equal the one the confirm counted, else `StateConflictError`                                                                                                                                                                                               |
| `workspace rehome` confirm                                                                             | under `rehome.py:96`, at `:217`                                                                                                           | before; it shows only the stored and new paths; re-read the path and re-run the probes after acquiring                                                                                                                                                                                                   |
| `session delete` confirm                                                                               | under `_queries.py:154`, at `:227`                                                                                                        | before; re-read the session uuid after                                                                                                                                                                                                                                                                   |
| empty-console offer (session delete, remove-sessions)                                                  | under the guard (`_queries.py:295`, `crud.py:494`, `_resource_cleanup.py:97`)                                                             | before, phrased as "will be empty"; under the console's lock a consented console is deleted only if still empty; a console that became empty without an offer is kept with a note                                                                                                                        |
| session delete's workspace and agent offers                                                            | unlocked (`_queries.py:159-160`)                                                                                                          | unchanged; the deletes they run take their own sets and re-check emptiness under them                                                                                                                                                                                                                    |
| `console delete` confirm                                                                               | under `attach.py:854`, at `:856`                                                                                                          | before; re-read the console after                                                                                                                                                                                                                                                                        |
| restart consent (`session restart`)                                                                    | before, `_lifecycle.py:411`; refusal under the guard `_runtime.py:229-234`                                                                | unchanged                                                                                                                                                                                                                                                                                                |
| restart consent (`session start --spec`)                                                               | under `_spec.py:128`, at `:135`                                                                                                           | before, as `session restart` does; the same refusal under the set if the session became running without consent                                                                                                                                                                                          |
| `runtime_state` gate (infrastructure) secrets                                                          | under the families (`_prepare.py:213-221`)                                                                                                | per family before `acquire_families`; a failure still blocks only that family                                                                                                                                                                                                                            |
| `runtime_state` workload union; console pane secrets (start, create, add-shell, add-sessions, restore) | under the families or the console guard (`runtime_state/_execute.py:45`, `lifecycle.py:117-118`, `crud.py:379`, `:696`, `restore.py:353`) | stays inside: which secrets are needed depends on definitions and observations the locked phase prepares ("healthy no-ops do not request unused launch inputs"). The set is only the family's (or the console's) own: the targets with their shared ancestors. The notice line precedes the first prompt |
| VM, workspace, agent choice prompts                                                                    | before (`cli/_helpers.py:196`, `_create_plan.py:175`, `:349`, `:404`, `copy.py:140`)                                                      | unchanged                                                                                                                                                                                                                                                                                                |

The notice line is `NativeLockSet.announce_prompt()`: before the first prompt asked while a set is
held, it prints one `output.info` line naming the held entities and saying they stay locked until
the answer is given. It prints once per set, and only when the command is about to prompt (a
resolver whose pending names all come from non-interactive sources prints nothing).

### Flows that need more than a row

**Session create.** Today the plan, graph build and gate all run under one VM guard. New: plan
(prompts) and graph build without a lock; `require_vm_ssh_boundary`, the gate and
`_preflight_and_resolve` (`_create.py:413-417`); then one acquisition of the whole request (session,
grant, and the new workspace or account exclusive in place of shared); then the existing reload
(`_create.py:378-386`) and `require_admitted`. The held set is attached to the graph so
`realize_workspace`, `realize_agent` and the session slice pass it as `held=`. Rollback runs inside
the set: `PendingAgentNode.teardown` and `PendingWorkspaceNode.teardown` call `delete_agent` and
`delete_workspace` with `held=` (`agents/nodes.py:219`, `workspaces/nodes.py:151`), whose requests
the set covers (the new account or workspace is held exclusive), and whose extensions find only this
create's own session and grants, all of which descend from an entity the set holds exclusive. So a
rollback is never refused; a test proves it.

**Agent and workspace delete.** Acquire the primary set, re-read the dependents, extend with each
found session (exclusive, with its other parent shared), each grant (likewise), and each console
holding a found session (exclusive, with its other members shared), then compare with what the
confirm showed. This replaces today's use of the pre-guard session list (`lifecycle.py:307`, used at
`:390-417`).

**Workspace delete versus agent delete on a shared session.** Workspace delete extends with the
session and its account shared; agent delete holds that account exclusive and extends with the
session exclusive. Whichever extends second is refused before any native work.

### runtime_state families

A family stays one VM's set (`_selection.py:92-128`). Its request:

| Selection                                       | Family request                                                                |
| ----------------------------------------------- | ----------------------------------------------------------------------------- |
| VM scope (`vm reconcile`, `select_vm_scope`)    | X vm (it may change power and covers every session and console on it)         |
| session scope (start, stop, restart, reconcile) | X each selected session on that VM; vm\*                                      |
| console scope (`console reconcile`)             | X the console; vm\*                                                           |
| held (`run_session_batch(held=)` from `--spec`) | the family request extends the held set; `_spec.py` picks vm\* when acquiring |

Before `acquire_families`, each family's site gate secrets resolve through the invocation's
resolver, recording a failure against that family exactly as `_prepare.py:213-221` does today. A
refused family is failed and blocked as today (`_selection.py:122-128`), now carrying the R3
message; the others proceed. Under a shared vm a family whose VM turns out stopped is blocked with
the "stopped after this command checked it" error instead of activating (`_prepare.py:247` calls
`held.require` first). A family whose VM was not confirmed active holds the vm exclusive for its
whole run, by decision: a held shared lock cannot be upgraded to boot, and dropping the exclusive
after the boot would mean releasing, reacquiring shared and re-validating the whole selection for a
case where concurrent work is rare, since little else runs on a VM that was stopped.

### Access commands that boot

`vm shell` on a stopped VM: the gate's fast path fails, `auto_start` takes `{vm: exclusive}`, boots,
releases; `hold_active` takes `{vm: shared}`, re-reads, enters `vm_active`, releases; the shell runs
holding no native lock, with the `vm_active` keepalive held for the span as today.

## 4. Shared state across entities

| State                                      | Mechanism                                                                                                                                                                                                                                                                                                 | Call sites                                                                                                                                                                                                                                                                                                                       |
| ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| VM account and group files                 | guest `flock` wrapper, below                                                                                                                                                                                                                                                                              | `agents/initializer.py:83`, `:172`, `:181`, `:421`; `agents/grants.py:257`, `:278`; `workspaces/guest.py:107`; `workspaces/backends/vm.py:139`, `:321`; `workspaces/manager/repair.py:187`, `:322`, `:329`; `workspaces/manager/copy.py:216`; `vms/initializer/shell_env.py:256`; `sessions/tmux.py:542`                         |
| `/opt/agentworks/tmux-session.conf`        | already an atomic replace: no change                                                                                                                                                                                                                                                                      | `sessions/tmux.py:711-722` writes through `Files.write`, which writes an `O_EXCL` temporary with a fresh name and renames it over the file (`execution/file_program.py:298-328`); parents tolerate a concurrent `mkdir` (`:126-129`). Callers `_create_roll.py:390`, `_execute.py:279`                                           |
| socket roots and per-account socket dirs   | already convergent: no change                                                                                                                                                                                                                                                                             | `ensure_agent_socket_root`, `ensure_agent_socket_dir`, `ensure_admin_socket_root` (`tmux.py:456-511`, `:607-632`): each step is `mkdir -p` or an absolute `chown`/`chmod`, so any interleaving of two runs ends in the same state. Called from `create_session` (`tmux.py:1042-1048`) under the session's set                    |
| stale socket cleanup                       | runs only under the account exclusive or the vm exclusive                                                                                                                                                                                                                                                 | `cleanup_stale_sockets` (`tmux.py:546`) from `create_agent_on_vm` (`agents/initializer.py:227`: agent create, reinit, `session create --new-agent`) and `_converge_agent_sockets` (`vms/initializer/driver.py:349`: vm create, reinit). No session of that account can launch meanwhile, since a launch holds the account shared |
| workspace group membership                 | the grant entity                                                                                                                                                                                                                                                                                          | every `add_to_workspace_group` / `remove_from_workspace_group` caller holds the grant exclusive                                                                                                                                                                                                                                  |
| set of grant-all agents, set of workspaces | the grant-all entity                                                                                                                                                                                                                                                                                      | [section 1](#entities)                                                                                                                                                                                                                                                                                                           |
| a workspace directory path                 | already reserved by an exclusive `mkdir`: no change                                                                                                                                                                                                                                                       | create (`workspaces/backends/vm.py:127`), rehome (`rehome.py:228`): two operations aiming at one path cannot both reserve it                                                                                                                                                                                                     |
| console membership                         | the console entity, with members as parents                                                                                                                                                                                                                                                               | every membership change holds the console exclusive; deletes that cascade it extend with it                                                                                                                                                                                                                                      |
| operator SSH config                        | workstation rebuild mutex, below                                                                                                                                                                                                                                                                          | `ssh_config.py:157` (`sync_ssh_config`) and so every caller (`vms/manager/lifecycle.py:699`, `host_keys.py:190`, `driver.py:143`, `recover.py:142`, `:243`, `reinit.py:414`, `power.py:323`, `:485`, `agents/realize.py:236`, `agents/manager/lifecycle.py:442`, `:850`, `cli/commands/config.py:112`)                           |
| VS Code workspace files                    | the same mutex under its own name, `rebuild_mutex("vscode-workspaces")`, around the full regeneration and around each per-file write or removal (which also run under their workspace's or the vm's exclusive), so a regeneration that read the database first cannot recreate a deleted workspace's file | `cli/commands/config.py:80` (regeneration); `workspaces/backends/vm.py:350`, `delete.py:263`, `power.py:308`                                                                                                                                                                                                                     |
| host-key store                             | per-VM file written atomically under the vm exclusive: no change                                                                                                                                                                                                                                          | `execution/trust.py:242-261`                                                                                                                                                                                                                                                                                                     |

### Guest accounts lock

One helper in `linux_users.py` builds every account or group edit:

```python
ACCOUNTS_LOCK_DIR = "/run/agentworks/locks"
ACCOUNTS_LOCK_WAIT = 30  # seconds
ACCOUNTS_LOCK_TIMEOUT_STATUS = 75

def account_edit(argv: Sequence[str], *, deadline: float | None = None) -> Command:
    """argv under the VM's account-file lock, as root."""
```

The command is
`sh -c 'mkdir -p -m 0700 "$0" && exec flock -E 75 -w 30 "$0/accounts" "$@"' /run/agentworks/locks <argv...>`,
elevated; `account_edit_script(source)` wraps a script the same way for the two script sites
(`workspaces/guest.py:107`, `sessions/tmux.py:542`). An exit status of 75 (`flock`'s conflict
status, set by `-E`; no shadow tool uses 75) raises
`StateError("the account files on VM '<vm>' stayed locked for 30 s", hint="Retry the command.")`
through one `require_account_edit` check; the edit itself never ran. `flock` is util-linux, which
Debian marks essential.

- The lock lives in a root-only directory: `flock` locks through a read-only descriptor, so a
  world-readable lock file would let any agent account hold it and stall every account edit on the
  VM. `/run` is tmpfs, so the wrapper creates the directory each time.
- The wait is bounded rather than fail-fast because this is a critical section around one short
  command, not an operation conflict, and it covers processes from other workstations too.
- Debian 12's `useradd` (shadow 4.13, checked on this host) links glibc's `lckpwdf`, which itself
  waits up to 15 s, so shadow already serializes its own tools there. The wrapper stays (decision
  3): it makes the guarantee agentworks's own across guest releases, covers the check-then-act
  scripts such as `getent group || groupadd` (`workspaces/guest.py:107`), and gives a typed timeout.
- Not wrapped: the platform bootstrap script and WSL2's create
  (`capabilities/vm_platform/bootstrap_script.py:107-111`, `wsl2.py:902`, `:909`), which run before
  the VM can be reached by any other operation, under `vm create`'s exclusive.

### Agent account removal

`delete_agent_on_vm` (`agents/initializer.py:390-425`, called from agent delete at
`agents/manager/lifecycle.py:435` and from agent create's rollback at `agents/realize.py:108`) runs
`userdel -r`, which would hold the accounts lock for as long as the home takes to delete. It splits
instead (decision 2), keeping its order of `pkill` and socket-directory removal first and its
warn-on-step-failure handling:

1. Probe, as root: the account's uid and home from `getent passwd` (`passwd_lookup`,
   `agents/initializer.py:44`), and the mail spool directory from `MAIL_DIR` in `/etc/login.defs`
   (default `/var/mail`).
2. `account_edit(("userdel", linux_user))` under the accounts lock. Without `-r`, `userdel` still
   removes the account's user-private group (`USERGROUPS_ENAB`) and the passwd, shadow and group
   entries; what `-r` adds is only the home and the mail spool, which the next step removes.
3. Outside the accounts lock, still under the account's exclusive lock that agent delete (and the
   creating operation, on rollback) holds: one root script removes the home with
   `rm -rf --one-file-system --` only when it is a directory still owned by the probed uid, and the
   spool `<MAIL_DIR>/<user>` only when it is a regular file owned by that uid. That keeps
   `userdel -r`'s rule of not removing a home the user does not own, and never follows into another
   file system. The account's lock is what makes the freed name safe meanwhile: no create of the
   same account can start until it is released.

A failure in step 3 warns through the existing `STEP_FAILURES` handler, as a `userdel -r` exit 12
(home or spool removal failed) does today.

### SSH config and other workstation rebuilds

`native_locks.mutex.rebuild_mutex(name)` is a workstation-wide exclusive file lock at
`CONFIG_DIR / ".locks" / name` (`config/validation.py:26`, the directory the host-key store already
uses), taken with the platform calls of section 1 by polling the non-blocking try every 50 ms for at
most 10 s. A timeout raises `StateError` naming the rebuild, with the hint
`agw config sync-ssh-config` (or `sync-vscode-workspaces`). `sync_ssh_config` takes
`rebuild_mutex("ssh-config")` around its whole body, so its database reads (`ssh_config.py:436`,
`:460`) and its writes (`:247`, `:363-381`) are one critical section and the last rebuild always
reads the latest committed state. It is per OS user rather than per database (decision 6) because
the SSH config is the OS user's file whatever the database. Callers keep their current failure
handling; `sync_ssh_config_or_warn` turns a timeout into its warning. The mutex is a leaf: nothing
inside it acquires a native lock.

## 5. Tests

New tests live beside the package (`tests/native_locks/`); every primitive test is marked
`pytest.mark.windows` so the Windows job (`.github/workflows/ci.yml:97-115`) runs it. Assertions
read the error's fields (`entity_kind`, `entity_name`, mode, `holder.command`, `holder.held_for`),
never its wording.

| Test                  | Proves                                                                                                                                                                                                                                                       |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| conflict matrix       | the four mode pairs on one entity, within a process (two sets) and across processes (a subprocess holder), on both platforms                                                                                                                                 |
| tree                  | a session change refuses a workspace change and the vm exclusive, and is refused by them; two sessions in different workspaces and accounts coexist; workspace delete and agent delete on a shared session refuse one another                                |
| ordering              | two contending single-pass acquisitions never both fail                                                                                                                                                                                                      |
| crashed holder        | a killed subprocess holder frees every entity (bounded poll on Windows) and its record is removed by the next reader                                                                                                                                         |
| old-version interlock | a subprocess running today's algorithm inline (`flock` exclusive, or `msvcrt.locking` after `seek(0)`) refuses new shared and exclusive acquisitions with the fallback (no holder), and a new shared holder makes the old algorithm fail with its busy errno |
| holder naming         | the longest conflicting live holder is named with its command, entity and age and the count of others; a stale record is skipped and deleted; a torn record falls back; non-conflicting records are ignored                                                  |
| record content        | invoking a command through the CLI runner with a distinctive argument value leaves a record whose fields contain no part of that value                                                                                                                       |
| extend failure        | a refused extension releases only its own entities: another process can take them, the earlier entities stay held, the record lists only the earlier ones                                                                                                    |
| nested reuse          | a covered request opens nothing new; a shared-to-exclusive request, a different VM or database, and a released set each raise `StateError`                                                                                                                   |
| activation            | `auto_start` under a shared set refuses instead of booting; with no set it boots under a transient exclusive; `require_admitted` refuses a changed vm_uuid, intent or host                                                                                   |
| accounts wrapper      | on Linux CI, the built command run locally with a temporary lock directory serializes two edits and maps a held lock to the typed timeout                                                                                                                    |
| rebuild mutex         | a second rebuild waits for the first and then reads the newer database state; a held mutex past the bound raises the typed error                                                                                                                             |
| grant-all             | workspace create and grant-all agent create on one VM: one is refused before native work, and after both finish (retry included) every grant-all agent has a grant for every workspace                                                                       |
| rollback              | a failing `session create --new-workspace --new-agent` unwinds without any refusal while unrelated holders exist on the VM                                                                                                                                   |
| account removal       | on Linux CI against a scripted guest: `userdel` runs through the accounts wrapper without `-r`, then the home and spool are removed only when owned by the probed uid, and a home owned by someone else is kept                                              |
| VS Code regeneration  | a regeneration that read the database before a workspace delete committed does not recreate the deleted workspace's file                                                                                                                                     |

One concurrent pair per FRD story, driven through the real manager functions with the scripted
guest: the first operation is paused at a known guest command (an event the fake guest waits on)
while the second runs on another thread.

| Story                                         | Pair                                                                            | Expect                                |
| --------------------------------------------- | ------------------------------------------------------------------------------- | ------------------------------------- |
| sessions sharing neither workspace nor agent  | `session create` paused; `session start` of another session                     | both succeed                          |
| agent create or reinit beside other agents    | `agent reinit` paused; `session stop` of another agent's session                | both succeed                          |
| workspace changes beside other workspaces     | `workspace delete` paused; `session start` in another workspace                 | both succeed                          |
| access during changes                         | `agent create` paused; `vm exec`, `session attach` and `session logs` admission | admitted                              |
| VM-wide refused while something below changes | `session start` paused; `vm stop`                                               | `vm stop` refused, holder named       |
| refusal names the holder                      | `workspace rehome` paused; `session start` in that workspace                    | refused naming rehome, workspace, age |

### Implementation-time check: harness setup confinement

The map assumes `apply_workspace_setup` and `apply_agent_setup` write only inside their owner (the
workspace tree, the account home); `apply_vm_setup` runs under the vm exclusive and is covered
whatever it writes. The implementation PR confirms this for every harness integration's writers
(decision 5), for each one either with a test that a setup applied for one owner leaves another
owner's tree untouched, or with a review cited in the PR naming the writer and the paths it can
reach. I checked only the cleanup paths (`harness_setup/cleanup.py:25-42`). A writer that crosses
owners adds the other owner to its request, exclusive, before the PR merges.

### Existing tests that change

| File                                                                                                                                                                                                                                                                                                                                                    | Why                                                                                  |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `tests/capabilities/test_native_setup_locking.py`                                                                                                                                                                                                                                                                                                       | replaced by `tests/native_locks/`                                                    |
| `tests/vms/test_vm_nodes.py` (`:436-511`)                                                                                                                                                                                                                                                                                                               | `auto_start` and admission tests move to the new activation semantics                |
| `tests/sessions/test_console_preparation.py` (`:139`, `:363`)                                                                                                                                                                                                                                                                                           | delete consent now precedes the lock; the monkeypatched guard target moves           |
| `tests/sessions/test_session_spec_launch.py`, `tests/vms/test_rekey_key_selection.py`, `tests/vms/test_recover.py`                                                                                                                                                                                                                                      | consent and auth-key prompts move before acquisition; recover's confirm stays inside |
| `tests/runtime_state/test_failures.py`, `test_composition.py`, `test_preview_trust.py`                                                                                                                                                                                                                                                                  | family busy reporting, held-set reuse, dry run still takes no lock                   |
| `tests/test_session_ephemeral_guard_unwind.py`, `tests/sessions/test_create_start_restart_orchestrated.py`, `tests/agents/test_create_reinit_orchestrated.py`, `tests/vms/test_lifecycle_orchestrated.py`                                                                                                                                               | held set instead of the guard token; acquisition after the gate                      |
| `tests/capabilities/test_native_setup_lifecycle.py`, `test_native_setup_deletion.py`, `test_native_setup_state.py`                                                                                                                                                                                                                                      | `held=` becomes a `NativeLockSet` with the owner's request                           |
| `tests/sessions/test_prepared_runtime.py`, `test_console_value_barrier.py`, `test_console_attach_orchestrated.py`, `tests/artifacts/test_session_lifecycle.py`, `tests/test_wsl2_keepalive.py`, `tests/vms/test_reinit_admin_options.py`, `tests/vms/test_delete_vm_gating.py`, `tests/test_legacy_claude.py`, `tests/orchestration/test_activation.py` | construct or patch the old guard; mechanical port                                    |

## 6. Collateral for the implementation PR

Every item states the current single-guard behavior and goes stale with this change; the HLA's
delivery list names the same set.

- `cli/command-reference.md:687-692`, the busy paragraph: hierarchical sets, what refuses what, the
  holder message, prompts before locks with their exceptions, admission still not spanning a body.
- `cli/agentworks/orchestration/README.md:166-170`: held sets replace the family guard and token;
  the two activation patterns.
- `docs/guides/upgrading-to-0.20.md`: an entry saying operations below the VM now run concurrently,
  a refusal names its holder, and an older agw still running during the upgrade interlocks safely
  but reports busy without naming holders.
- `docs/guides/desired-runtime-state.md:154`, `:161-165`.
- `docs/guides/native-harness-setup.md:336-340`, `docs/guides/harness-facets.md:400`,
  `docs/guides/idempotency.md:298`, `cli/agentworks/db/README.md:138`,
  `cli/agentworks/capabilities/harness_integration/README.md:376`: "VM-family mutation guard"
  becomes the owner's lock.
- `cli/agentworks/guide/guide-content/troubleshooting.md:55-77`, a guide topic: busy causes and the
  holder message.
- The `native_locks` package docstring carries the durable rules (tree, modes, order, no upgrade,
  records advisory) so they outlive this SDD.

No sample-config or completion change: the waits are constants and the command tree is unchanged.

## 7. Decisions

The lead ruled on the first draft's open questions (2026-11-23); each is folded in above.

1. Workload and pane secrets stay inside the lock, after one notice line naming the locked entities,
   which is R6's "says so"; the set held is only the family's ([Prompts](#prompts-r6)).
2. `userdel -r` splits: `userdel` under the accounts lock, then the home and mail spool removed
   outside it under the account's exclusive lock ([Agent account removal](#agent-account-removal)).
3. The guest `flock` wrapper stays ([Guest accounts lock](#guest-accounts-lock)).
4. A runtime family on a VM not confirmed active holds the vm exclusive for its whole run
   ([runtime_state families](#runtime_state-families)).
5. Harness setup confinement is an implementation-time check with a test or cited review per writer
   ([section 5](#implementation-time-check-harness-setup-confinement)).
6. The rebuild mutex is per OS user
   ([SSH config and other workstation rebuilds](#ssh-config-and-other-workstation-rebuilds)).

No question is open.

## Remaining differences from the HLA

Read against the amended HLA (4e4880d32). For the HLA's owner; this LLD follows the behavior it
states.

1. **Holder liveness.** The HLA's naming section keeps records "whose pid is alive" and deletes one
   "whose process is gone". This LLD decides liveness by the record's own lock instead
   ([Stale records](#stale-records)), which the OS releases at exit, so a reused pid cannot make a
   dead holder look alive and no platform-specific process probe is needed. The pid stays in the
   record for diagnosis only.
2. **Order.** The HLA's global order (vm, workspaces, accounts, grants, sessions, consoles) omits
   `grant-all`. This LLD places it after accounts and before grants; anywhere after vm keeps the
   property that ancestors precede descendants.
3. **Error example.** The HLA's example omits the VM and ends in a period; this LLD's message names
   the VM (`session/build-42 on VM 'dev' is blocked: ...`), since one name can be locked on several
   VMs and an operator may run commands against more than one.
