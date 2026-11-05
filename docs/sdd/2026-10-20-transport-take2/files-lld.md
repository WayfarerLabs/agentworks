# Transport Take 2: Files Low-Level Design (Slice 5)

- Status: Draft for the private review loop.
- Date: 2026-11-05
- Architecture: [hla.md](./hla.md) D5 (`PythonProgram`, input and sinks), D6 (files), D12 (delivery
  to consumers), D13 (errors and the operation log), D14 (the guards); requirements: R4, R5, R9,
  R10, R11
- Plan: [plan.md](./plan.md), slice 5, delivered in PR C. Slices 6 and 7 consume the same
  operations.
- Source baseline: `e645220ad` (the PR B branch head). Every `path:line` is at that commit. Paths
  are under `cli/agentworks/` unless they start with `cli/` or `docs/`.

## Scope

This LLD settles slice 5: the file operations built on `run`, the request vocabulary they need, the
`RunContext` accessors, and the migration of the callers the plan names onto both.

- the file program: each operation's arguments and its use of stdin and stdout;
- the lock, the symlink policy, mode and ownership, and cleanup;
- the workstation API: signatures, results, errors, `update`'s loop, digests and size limits;
- finite stdin, `PythonProgram`, `Script.interactive` and the buffered sinks;
- `RunContext.admin_access()` and `agent_access()`;
- the migration per caller, tests, the incident audit, collateral and a delivery cut.

**Operator direction applied.** "Please, please, please don't take on a bunch of extra complexity"
(FRD, 2026-11-01). Every operation below has a caller in this slice or a named one in slice 6 or 7.
Where the HLA leaves a choice open the smallest one is taken, and the alternative is an open
question at the end. Hostile writers stay with the permissions effort (R5); this design guarantees
nothing against them beyond refusing links.

**What exists at HEAD.** No `PythonProgram`, `Bytes`, sink type or `Script.interactive` exists
(`execution/requests.py:48-124`). `VMAccess.run` closes ssh's stdin right after the request and
captures each stream up to the pump's 1 MiB (`execution/access.py:94-114`, `execution/pump.py:37`,
`:93-104`). The file program is `native_files.py`'s `_FILE_PROGRAM` (`native_files.py:22-189`), sent
as `python3 -c` argv with a scp staging directory (`:258-298`, `:338`, `:371`).

**Out of this slice.** The HLA lands each item with its first consumer (D5, R10), so three
operations designed here land later: `write` and `upload_tree` in slice 6 (initializer fragments and
`sources.fetch_dir`, `sources.py:239-289`) and the unconditional `remove` in slice 7 (workspaces).
Every slice 5 write is conditional. `FromFile`, `ToFile` and the `observation` option stay slice 7.

## Module layout

| Module                                                            | Contents                                                                                                                                                |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `execution/requests.py`                                           | `NoInput`, `Bytes`, `stdin` on `BoundedRequestOptions`, `PythonProgram`, `Script.interactive`, `Capture`, `Discard`; `RunRequest` gains `PythonProgram` |
| `execution/envelope.py`                                           | The `python` kind, the `interactive` flag, caller input after the envelope frame                                                                        |
| `execution/agw_launch.py`                                         | Runs a `python` payload and both interactive startups                                                                                                   |
| `execution/access.py`                                             | `run(request, *, stdout=, stderr=, check=)`, the `files` property, the withheld streams passed to the log                                               |
| `execution/oplog.py`                                              | The `PythonProgram` shape and the `withheld` streams                                                                                                    |
| `execution/file_program.py` (new)                                 | The guest file program: plain syntax, standard library only, importable                                                                                 |
| `execution/files.py` (new)                                        | `Files`, `guest_path`, the result types, `update`, `program_source()`                                                                                   |
| `execution/errors.py`                                             | `FileConflict`, `PathRefused`, `FileLockTimeout`                                                                                                        |
| `capabilities/base.py`                                            | `RunContext.admin_access()` and `agent_access()`                                                                                                        |
| `command_checks.py`                                               | On `VMAccess`                                                                                                                                           |
| `artifacts/publication.py`                                        | Gains `ROOT_FILE_DIRECTORIES` and the root path rule from `native_files.py:193`, `:229-238`                                                             |
| `capabilities/harness_integration/setup.py`                       | `SetupInvocation.access`                                                                                                                                |
| The migrated callers                                              | As the migration table says                                                                                                                             |
| `native_files.py`, `harness_setup/runner.py`                      | No caller left; deleted here or in slice 8 (open questions 6 and 3)                                                                                     |
| `cli/tests/execution/test_file_program.py`, `test_files.py` (new) | The program against a real local filesystem, and the API over the L lane                                                                                |
| `cli/tests/vm_access_support.py`                                  | `RecordingVMAccess` handles `files`; a local access over the L lane for caller tests                                                                    |

`file_program.py` is a separate payload from agw-launch, as D6 requires: `files.program_source()`
reads it from the installed package as `envelope.launcher_source()` reads `agw_launch.py`, and only
a file operation sends it. It has no import-time effect. It defines
`main(argv, *, lock_path=LOCK_PATH, lock_wait=LOCK_WAIT_SECONDS) -> int` and ends with
`if __name__ == "__main__": sys.exit(main(sys.argv[1:]))`, so the tests import it and call `main`
with a temporary lock, and the package guard can import it on any workstation (the `fcntl`, `pwd`
and `grp` imports sit inside `main`, as agw-launch keeps `pwd` out of module level).

## Requests this slice adds

The slice 2 vocabulary (`execution-lld.md`, "Requests and results") left these to their first
consumer. Every one has one here.

### Input

`BoundedRequestOptions` (so `Command`, `Script` and `PythonProgram`) gains
`stdin: NoInput | Bytes = NoInput()`. `LoginShell` has none, so incident row I4's refusal of input
with a terminal stays structural.

- **`Bytes(data: bytes, sensitive: bool = False)`**, `data` `repr=False`. The client writes `data`
  after the envelope frame, as D3's third part, then closes ssh's stdin. `PipeSession` already takes
  the request bytes and an optional stream (`pump.py:211-237`); `Bytes` is appended to the request
  bytes, so the pump needs no change. Nothing bounds it but memory (D3).
- **Sensitive.** `sensitive=True` keeps both streams out of the operation log entirely (D13): the
  record says they were withheld. Input never reaches the log in any case, since `format_record` is
  never given it (`execution/oplog.py:23-37`). Errors and `repr` never hold it.
- **`stream`** takes the operator's stdin as its own argument (`access.py:116`), so it raises
  `ValueError` before dispatch for a request whose `stdin` is not `NoInput`. Every caller of
  `stream` is first-party code, so this is an interior check, not a boundary.

### Buffered sinks

`run(request, *, stdout: Capture | Discard = Capture(), stderr: Capture | Discard = Capture(), check=False)`.

- **`Capture(limit: int = 1_048_576, sensitive: bool = False)`** keeps up to `limit` bytes; more is
  `Unknown(OUTPUT_LIMIT)`, as today. A sensitive capture reaches the caller's `Result` and is
  withheld from the log. Open question 1 covers why this flag exists.
- **`Discard()`** keeps nothing; the stream still runs through its filter, so the markers and the
  trailer are read. Its `Result` field is empty. Consumers: `command_checks.py` and the native CLI
  (D5's table).

**The log.** `run` computes `withheld`, the streams the log must not see (both for a sensitive
stdin, one per sensitive capture), and calls `log(request, result, withheld=withheld)`.
`format_record(request, result, *, label, withheld=())` writes `stdout: withheld` in place of a
withheld stream. `SSHLogger.record` passes the keyword through. This is the one change to slice 3's
log signature.

### `PythonProgram`

`PythonProgram(source: str, args: tuple[str, ...] = (), login: bool = False, interactive: bool = False)`
with the bounded options. `source` and `args` are `repr=False`, as `Script.source` and
`Command.argv` are.

- **Envelope.** `kind: "python"` with `source`, `args`, `login` and `interactive`. `envelope.encode`
  validates `args` as it does argv (no NUL, each element at most 131,071 bytes once encoded,
  surrogate rules) and `source` as script text.
- **agw-launch** writes the source to the same sealed memfd a script uses (`execution-lld.md`,
  agw-launch step 5) and runs, with `N` the memfd's descriptor:
  - neither flag: `/usr/bin/python3 -I -S /dev/fd/N *args`;
  - either flag:
    `<passwd shell> [-l] [-i] -c 'exec /usr/bin/python3 -I -S "$0" "$@"' /dev/fd/N *args`. The `-c`
    string is constant, the descriptor path is `$0` and the arguments are positional, so nothing is
    quoted (D5).
- **Measured** at the baseline on Debian 12.15 (python3 3.11.2) with `sh`, `dash`, `bash` and `zsh`:
  the program sees `a` and `b c` as its two arguments, and the interactive shells' job-control
  notices land on stderr only. A conformance vector repeats this on each lane (below).
- **Why `/usr/bin/python3`.** The probe and the native CLI's resolution ran `python3` from the login
  `PATH` (`artifacts/native/probe.py:402-410`), so a mise-managed Python could serve them. They now
  always get the system interpreter that agw-launch already proves, with its standard library. A
  behavior change for release notes.

### `Script.interactive`

`Script` gains `interactive: bool = False`. With it, agw-launch runs
`<interpreter> [-l] -i -c '. "$0"' /dev/fd/N`, sourcing the memfd from an interactive shell. Slice 2
measured that `dash -i FILE` prints a prompt per line it reads (`execution-lld.md`, "Left to the
first consumer"); `-c` reads no commands from a file or stdin, so it prompts for nothing. Measured
at the baseline under all four shells: a sourced `exit 7` is the shell's status, and `HOME` is set.
The consumers are the launch-target probes, which run `"$SHELL" -lic '<inner>'` today
(`plugins/claude/harness_integration.py:525`, `plugins/codex/harness_integration.py:1166`, `:1224`,
`:1321`, `plugins/grok/harness_integration.py:384`, `plugins/claude/artifacts.py:186-192`,
`command_checks.py:27-49`).

**Operation log shapes.** `python program (login, interactive), args <shlex.join(args)>` and
`script (user_default, login, interactive)`, then the common parts. The program's source is never
written.

## The file program

Every operation is one `run` of one `PythonProgram`: `source` is `program_source()`, `args` is
`(op, params)` with `params` one JSON object
(`json.dumps(..., ensure_ascii=True, separators=(",", ":"))`), and `elevated` is the caller's flag.
JSON keeps every parameter named and carries any path byte the way the envelope does
(`surrogateescape`). Parameters are paths, modes, account names and digests; content is never an
argument. Open question 7 covers the alternative.

| Op                    | Params                                                                  | stdin   | stdout                       | Lock                |
| --------------------- | ----------------------------------------------------------------------- | ------- | ---------------------------- | ------------------- |
| `read`                | `path`, `limit`                                                         | none    | the file's bytes             | none                |
| `stat`                | `path`                                                                  | none    | `{"kind", "mode", "digest"}` | none                |
| `write_if`            | `path`, `expected`, `mode`, `owner`, `group`, `parent_mode`, `preserve` | content | `{}`                         | compare and rename  |
| `remove_if`           | `path`, `expected`                                                      | none    | `{}`                         | the whole operation |
| `ensure_directory`    | `path`, `mode`, `group`                                                 | none    | `{"created"}`                | the whole operation |
| `prune_empty_parents` | `path`, `root`                                                          | none    | `{"root_retained"}`          | the whole operation |
| `write` (slice 6)     | as `write_if` without `expected` and `preserve`                         | content | `{}`                         | rename              |
| `upload_tree` (6)     | `dest`                                                                  | tar     | `{"replaced", "leftover"}`   | the swap            |
| `remove` (slice 7)    | `path`                                                                  | none    | `{"removed"}`                | the whole operation |

- **stdout** is the file's bytes for `read` and one JSON line otherwise. The program prints nothing
  before it, and agw-launch's filter drops everything before the start marker.
- **stderr** carries only a Python traceback, for status 1.
- **Statuses.** The program's own exit status is the answer:

| Status | Meaning                                                                                       | Workstation result         |
| ------ | --------------------------------------------------------------------------------------------- | -------------------------- |
| 0      | Done                                                                                          | the parsed result          |
| 1      | Uncaught exception                                                                            | `CommandFailed` (existing) |
| 2      | Conflict: the expectation failed, or a directory above the target changed (below)             | `FileConflict`             |
| 3      | `prune_empty_parents` kept an empty root its parent will not release (`native_files.py:73`)   | `Pruned.ROOT_RETAINED`     |
| 4      | A link or wrong type at the destination, a parent, or the lock file (`native_files.py:36-37`) | `PathRefused(UNSUITABLE)`  |
| 5      | `EACCES`, `EPERM` or `EROFS` (`native_files.py:38-39`)                                        | `PathRefused(DENIED)`      |
| 6      | `read` of a file larger than `limit`                                                          | `PathRefused(TOO_LARGE)`   |
| 7      | The lock was not acquired within its wait                                                     | `FileLockTimeout`          |
| 8      | `read` of an absent file                                                                      | `None`                     |

Statuses 2 to 5 keep `native_files.py`'s numbers, so `a2eaba12e`'s classification carries over
unchanged. Any outcome other than `Completed` raises through `Result.require_completed` first
(`execution/results.py:84-96`), so transport failures keep their D13 types.

### Walking a path

`files.guest_path(path)` is the one validator, at the workstation boundary: absolute, printable, and
no empty, `.` or `..` component, as `native_path` checks today (`native_files.py:218-226`). It
raises `StateError` as `native_path` does, since publication uses it on plugin output and recorded
paths (`artifacts/publication.py:59-67`, `:89-100`). The program receives only validated paths and
does not re-check them (interior).

- **Parents.** The program opens `/` with `O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC` and each
  parent component relative to the last, with the same flags (`native_files.py:32`, `:82-99`). A
  symlink component fails with `ELOOP` or `ENOTDIR` (status 4). `O_PATH` needs only search
  permission, so a search-only ancestor works (incident row I36).
- **A missing parent.** `read`, `stat` and `remove_if` report the target absent. `write_if` and
  `ensure_directory` create it: `mkdir` relative to the parent descriptor with mode 0700, then an
  `O_RDONLY | O_DIRECTORY | O_NOFOLLOW` open of what was just made, `fchown(-1, gid)` when a group
  is given, then `fchmod(parent_mode)` (for `ensure_directory`, its `mode`) so the umask plays no
  part (`native_files.py:91-97`). `EEXIST` from `mkdir` means another writer made it: the program
  opens it with `O_NOFOLLOW` and walks on.
- **The destination.** `read`, `stat` and the compare open it with
  `O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC` (`native_files.py:109`) and `fstat` it: a regular
  file is read; for `stat`, a directory is reported as one; a symlink (`ELOOP`) or any other type is
  status 4. `O_NONBLOCK` keeps a FIFO from blocking the open.
- **Before a rename or unlink**, under the lock, the destination name is `lstat`ed relative to its
  parent descriptor: absent or a regular file proceeds, anything else is status 4. A rename never
  replaces a link, and an unlink never removes one.
- **The parent's identity.** A staged operation holds its parent descriptor from before the lock.
  Under the lock it walks from `/` again and compares the parent it reaches with the one it holds by
  `(st_dev, st_ino)`. A mismatch is status 2. This is how a `write_if` staged in a directory that a
  tree upload (slice 6) or a workspace rehome (slice 7) then moved aside fails, instead of
  publishing into the old tree. A parent that vanished during staging (`ENOENT` on the temporary
  file) is also status 2.

**Left to the permissions effort** (R5): a writer that does not take the lock can still rename or
replace a parent directory, not a link, between the re-walk and the rename, or change the
destination between the compare and the rename; it can create the lock file with a mode others
cannot open, or hold the lock to stall them (D6); a hard link it owns into a destination directory
is followed by `read` (Debian's `fs.protected_hardlinks` limits it to files it can already write);
and an elevated operation walks directories an agent may be able to write. The elevated allowlist
(`ROOT_FILE_DIRECTORIES`) is caller policy in `artifacts/publication.py`, not enforcement in the
transport (FRD scope: no core file-mutation allowlist).

### The lock

One file, `LOCK_PATH = "/run/lock/agentworks-files.lock"`, for every mutating operation of every
identity (D6). The program acquires it in this order:

1. `open(LOCK_PATH, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)`.
2. On `ENOENT`: `open(LOCK_PATH, O_RDONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o644)`, then
   `fchmod(fd, 0o644)` whatever the umask.
3. On `EEXIST` from step 2 (it lost the race): step 1 once more.
4. `fstat` must show a regular file. A symlink (`ELOOP` in step 1) or any other type is status 4,
   and a denial status 5, each naming the lock file.
5. `flock(fd, LOCK_EX | LOCK_NB)`, retried every 50 ms until `LOCK_WAIT_SECONDS` (30) have passed,
   then status 7. Closing the descriptor, or the process ending, releases it.

The order is D6's: under `fs.protected_regular=2` an `O_CREAT` open of another identity's file in
the sticky `/run/lock` fails with `EACCES` even for root (measured on Debian 12 and 13, integration
delta on #856), and with `O_EXCL` the kernel reports `EEXIST` before it applies that rule, so step 2
fails only when the file exists. `flock` needs no write access, so a read-only descriptor serves
every identity. Measured at the baseline host: `/run/lock` is `drwxrwxrwt root root` and
`protected_regular` is 2.

**What each operation holds it for:**

- `read` and `stat` take no lock. A rename replaces the inode atomically, so a reader sees the old
  file or the new one whole.
- `write_if` stages without the lock: the temporary file, its content, owner, group and mode, and
  `fsync`. It then takes the lock, re-walks and compares the parent, checks the destination's type,
  compares the digest, copies metadata when asked, renames, `fsync`s the directory, and exits.
- `remove_if`, `ensure_directory` and `prune_empty_parents` take the lock first and hold it for the
  whole operation, which is a walk and a few system calls.
- `upload_tree` (slice 6) extracts without the lock and holds it only for the two renames.

**The wait.** The longest hold is a compare of an existing file (SHA-256 of a 16 MiB artifact member
takes tens of milliseconds) plus an attribute copy, so 30 s only expires when a writer is stuck or
hostile. Requests carry no client deadline: the program bounds its own wait, and the VM policy's
keepalives end a dead peer (`execution-lld.md`, liveness options). Open question 8 covers the value.

### Mode and ownership

`write_if` (and slice 6's `write`) applies everything before the rename:

1. Create `.agentworks-tmp-<24 hex>` relative to the parent descriptor with
   `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC`, mode 0600.
2. Copy stdin into it in 256 KiB chunks, so the guest never holds the content in memory.
3. `fchown(uid, gid)` with each `-1` unless `owner` or `group` names an account, resolved on the
   guest with `pwd.getpwnam` and `grp.getgrnam` (`native_files.py:146`). Then `fchmod(mode)`; chown
   first, because it clears set-id bits.
4. `fsync`.
5. Under the lock, with `preserve` and an existing destination: `fchown` to the existing owner and
   group, `fchmod` to its mode, then remove every extended attribute the temporary file has that the
   existing file lacks (an ACL inherited from a default directory ACL) and set every one the
   existing file has, ACLs included (`system.posix_acl_access` is an extended attribute). The order
   is `native_files.py:155-172`'s, so an ACL's mask survives the `fchmod`. A failure here exits
   before the rename, leaving the original untouched
   (`tests/artifacts/test_generated_sections.py:226-247`).
6. `rename` over the destination, then `fsync` the directory (`native_files.py:174-179`).

- **Unelevated**, `owner` must be the identity's own account or absent, and `group` one the identity
  belongs to; the kernel refuses anything else (status 5). Workspace settings and artifacts use the
  workspace group this way today (`plugins/_harness_native/native.py:145`,
  `harness_setup/dispatch.py:215`).
- **Elevated** placement is the request's `elevated` flag on each call (D6), so new files and
  directories are root's unless `owner` and `group` say otherwise. No caller writes a copy, chmod or
  sudo wrapper. An agent-bound access refuses an elevated operation before dispatch with
  `PermissionDenied` (D12), because `run` does.
- **Agent writes** use the agent's `VMAccess`: the program runs as the agent's account, so the
  kernel applies that account's permissions.
- **Modes are the caller's.** `files.py` has no mode policy: `publication.py` keeps `_mode`
  (`publication.py:221-224`) and gains `_directory_mode` (0755 elevated, 2770 with a group, else
  0700, from `native_files.py:91-97`), and native settings pass 0600 with parents 0700, or 0660 with
  parents 2770 and the workspace group (`docs/guides/native-harness-setup.md:51-53`).

### Cleanup

- **`write_if`.** A `finally` unlinks the temporary name, ignoring `ENOENT` after a successful
  rename (`native_files.py:180-184`). The program turns `SIGHUP` and `SIGTERM` into `SystemExit`, as
  the parked launcher does, so a dropped connection still runs the `finally`; Python already ignores
  `SIGPIPE`, so a closed stdout is an exception that runs it too. Directories the operation created
  stay: another writer may already be using them, removing them would race it, and retirement has
  `prune_empty_parents` for exactly this. R5 asks that cleanup touch only what the operation
  created, which keeping them satisfies.
- **`remove_if`, `ensure_directory`, `prune_empty_parents`** create nothing to clean up.
- **`upload_tree`** (slice 6) removes its staging tree on any failure before the swap, and after the
  swap removes the old tree outside the lock; a failure to remove it is reported as `leftover`, not
  as a failed upload, because the new tree is in place.
- **An ambiguous outcome.** A write whose `run` ends `Unknown` may or may not have renamed. `files`
  raises `ExecutionIncomplete` and nothing retries it, `update` included (R4).

### `upload_tree` (designed here, slice 6)

`upload_tree(local_dir, dest, *, elevated=False) -> TreeUpload` replaces `dest` with the contents of
`local_dir`, as `fetch_dir` does today with `rm -rf` and a tar copy (`sources.py:281-289`,
`transports/base.py:241-281`).

- **Workstation.** `tarfile.open(fileobj=buffer, mode="w:gz")` and
  `tar.add(local_dir, arcname=".")`, as `copy_dir_to` builds it (`transports/base.py:260-261`), sent
  as `Bytes(archive, sensitive=True)`. Nothing is extracted on the workstation.
- **Guest.** `tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz")` streams the members into a
  directory `.agentworks-tree-<24 hex>` beside `dest`, created 0700. Debian 12's python3 (3.11.2 at
  the baseline) has no `tarfile.data_filter`, so the program validates each member itself: a
  relative name with no `..`, `.` or empty component; a regular file, a directory or a symlink (hard
  links, devices and FIFOs are refused, status 4); permissions `mode & 0o777`; the modification time
  kept. Each member's parents are walked from the staging directory's descriptor with `O_NOFOLLOW`,
  so a later member can never be written through an earlier symlink member. Ownership is the
  extracting identity's, as a non-root `tar -x` gives today.
- **Swap.** Under the lock: re-walk and compare the parent; `lstat` `dest`. Absent: rename the
  staging tree to `dest`. A directory: rename `dest` to `.agentworks-old-<24 hex>`, then the staging
  tree to `dest`, renaming the old tree back if the second rename fails. Anything else, a symlink
  included, is status 4. Today's `rm -rf` removes whatever is in the way
  (`transports/base.py:267-270`); refusing it instead is a behavior change for slice 6's release
  notes. Python exposes no `renameat2(RENAME_EXCHANGE)`, so `dest` is absent for the instant between
  the renames; nothing reads a source tree while an initializer is writing it.
- **After.** Outside the lock, `shutil.rmtree(old, dir_fd=parent)`, which never follows symlinks.
- **Result.** `TreeUpload(replaced: bool, leftover: str | None)`. `fetch_dir` keeps its "overwriting
  existing" warning (`sources.py:283-285`) from `replaced`, and warns with the leftover path.

## The workstation API

`VMAccess.files` returns `Files(self)`, building nothing else (R9). `Files` methods:

```python
def read(path: str, *, limit: int = 1_048_576, elevated: bool = False) -> FileContent | None
def stat(path: str, *, elevated: bool = False) -> PathState
def write_if(path: str, data: bytes, *, expected: str | None, mode: int, parent_mode: int,
             owner: str | None = None, group: str | None = None,
             preserve_metadata: bool = False, elevated: bool = False) -> None
def update(path: str, transform: Callable[[bytes | None], bytes | None], *, mode: int,
           parent_mode: int, group: str | None = None, preserve_metadata: bool = False,
           limit: int = 1_048_576, elevated: bool = False) -> bool
def remove_if(path: str, *, expected: str, elevated: bool = False) -> None
def ensure_directory(path: str, *, mode: int, group: str | None = None,
                     elevated: bool = False) -> bool
def prune_empty_parents(path: str, *, root: str, elevated: bool = False) -> Pruned
# Slice 6: write(path, data, *, mode, parent_mode, owner=None, group=None, elevated=False) -> None
# Slice 6: upload_tree(local_dir: Path, dest: str, *, elevated=False) -> TreeUpload
# Slice 7: remove(path, *, elevated=False) -> bool
```

- **`read`** returns `FileContent(data, digest)` (`data` `repr=False`) or `None` when the file or a
  parent is absent. Its stdout is a `Capture(limit, sensitive=True)`, so the content never reaches
  the log.
- **`stat`** returns `Absent()`, `Directory(mode)` or `RegularFile(digest, mode)`, with `mode` the
  permission bits (`stat.S_IMODE`). Open question 2: this operation is not in the HLA's table.
- **`write_if`** publishes only while the destination still has digest `expected`, or is still
  absent for `None`. Content travels as `Bytes(data, sensitive=True)`, so the record withholds both
  streams, a failed write's traceback included; the error still names the operation, the path and
  the status.
- **`remove_if`** removes a regular file whose digest is `expected`. An absent file is a conflict,
  as `native_files.py:140-141` treats it today, because the caller observed it present.
- **`ensure_directory`** creates the directory and its missing parents with `mode` and `group`, and
  returns whether it created the last component. It never changes an existing directory's mode.
- **`prune_empty_parents`** removes the empty parents of `path` from the deepest up to and including
  `root`, which must be a proper ancestor (refused before dispatch otherwise,
  `native_files.py:411-412`), and stops at the first non-empty one (`native_files.py:41-81`).
  `Pruned.ROOT_RETAINED` is the verified-empty root its parent would not release; the caller warns,
  as `native_files.py:415-418` does.

**Errors**, in `execution/errors.py`. Each names the VM, the operation and the path, never content
or a stream (D13).

| Error                  | Kind         | Raised for                                                                                     |
| ---------------------- | ------------ | ---------------------------------------------------------------------------------------------- |
| `FileConflict`         | `StateError` | Status 2, and `update` after its last attempt                                                  |
| `PathRefused(reason)`  | `StateError` | `UNSUITABLE` (status 4), `DENIED` (5), `TOO_LARGE` (6, or `Unknown(OUTPUT_LIMIT)` on a `read`) |
| `FileLockTimeout`      | `StateError` | Status 7; the hint says another Agentworks operation is writing on the VM                      |
| `CommandFailed`        | existing     | Status 1 or any status outside the table                                                       |
| D13's transport errors | existing     | Any completion other than `Completed`, through `require_completed`                             |

`PathRefused(UNSUITABLE)` carries today's hint (`native_files.py:204-208`): Agentworks never reads
or writes through a link, and dotfile installers are the usual cause. The per-surface subject
today's messages add ("native settings file", "artifact destination", `native_files.py:199-215`)
goes: the path already says which file failed, and no caller branches on the subject. Its test
(`cli/tests/test_native_path_failures.py:46`) goes with it.

### `update`

D6's read, transform on the workstation, conditional write, retry on conflict:

```python
for _ in range(UPDATE_ATTEMPTS):  # 3
    current = self.read(path, limit=limit, elevated=elevated)
    before = None if current is None else current.data
    after = transform(before)
    if after is None or after == before:
        return False
    try:
        self.write_if(path, after, expected=None if current is None else current.digest, ...)
        return True
    except FileConflict:
        continue
raise FileConflict(...)
```

- **`transform`** may run more than once, always on the current content. It returns the new bytes,
  or `None` to leave the file alone. An exception from it (a malformed existing document) propagates
  with nothing written.
- **Unchanged content writes nothing**, so the file keeps its permissions (`native.py:122-123`,
  incident row I47).
- **Only `FileConflict` retries.** `FileLockTimeout`, refusals and every transport outcome
  propagate, since a lock held for 30 s will not clear in milliseconds and an `Unknown` write must
  not be dispatched again (R4).
- **The merge stays on the workstation** (D6). `native.py`'s transform calls
  `capabilities/harness_integration/settings.py`, which keeps JSON null as a value and rejects
  malformed input (`settings.py:78-104`, `:125-144`), so R5's merge rules hold by construction.

### Digests and size limits

- **SHA-256**, lowercase hex: the form `OwnedArtifactFile.sha256` records and `publication.py:184`
  computes, so recorded ownership needs no migration.
- **Where.** `read`'s digest is computed on the workstation from the bytes it received (D6).
  `stat`'s, and the comparisons `write_if` and `remove_if` make, are computed by the program, the
  comparisons under the lock.
- **`read`** refuses a file whose `fstat` size exceeds `limit` (status 6) before sending a byte; a
  file that grows past it during the read ends in `Unknown(OUTPUT_LIMIT)`, also `TOO_LARGE`. The
  default is the pump's 1 MiB. `publication.py`'s generated-section reads pass the artifact member
  bound, `DEFAULT_CAPTURE_LIMITS.member_bytes` (16 MiB, `package_sources.py:27-36`).
- **Writes** have no limit but the workstation's memory (D3), and the program streams them to disk.
  A caller that ever needs more uses slice 7's `FromFile`.
- **`stat`** transfers no content, which is why publication uses it: an artifact package can total
  64 MiB (`package_sources.py:31`).

## Capability delivery: `admin_access()` and `agent_access()`

`RunContext.__init__` gains `admin_access: VMAccess | None = None` and
`agent_access: VMAccess | None = None`, stored privately like the targets
(`capabilities/base.py:202-224`), with pass-through accessors beside `admin_target()` and
`agent_target()` (`:226-234`).

- **Lifecycle.** An access is present exactly when the same identity's target is, at the same timing
  (`capabilities/base.py:166-176`): preflight sees the identities that already exist, runup the
  current ones. Each construction site builds it from the row it already holds with
  `execution.vm_access(vm, config, Admin() or Agent(name, linux_user), log=logger.record)` where it
  builds or receives the target today. Construction copies names and paths and nothing else, so a
  context still connects to nothing, starts no VM and resolves no secret (R9).
- **Construction sites**, each passing the access beside its target:
  `vms/manager/lifecycle.py:317-330` and `vms/manager/reinit.py:212-222` (their `scoped_ctx`),
  `agents/manager/lifecycle.py:216-228` and `:660-670`, `sessions/manager/_create.py:200-208`,
  `sessions/manager/_create_roll.py:62-70`, `:222-227` and `:330-335`,
  `sessions/manager/_lifecycle.py:744-751` and `:880-885`, and `git_credentials/__init__.py`'s
  `ScopedContextFactory` (`:39-48`), `CredentialRequest.context` (`:63-77`),
  `materialize_credential_state` (`:107-142`) and `configure_user_git_credentials` (`:145-165`),
  whose callers in the initializers pass an access beside the target. These callers are slice 6 and
  7 code still on the legacy stack; building a `VMAccess` there is legacy code calling new code,
  which R10 allows.
- **While both exist**, `RunContext.__init__` raises `StateError` when an access is given without
  its identity's target or the reverse, so a half-wired site fails at construction. Open question 4
  proposes deleting the targets in this slice instead, which removes the check with them.
- **What plugin authors see.** `ctx.admin_access()` and `ctx.agent_access()` return a `VMAccess`
  bound to that identity, or `None`. They build requests from `agentworks.execution.requests`, call
  `run`, and use `access.files`. The agent's access refuses `elevated=True` before dispatch with
  `PermissionDenied`. Results are typed, so a probe tells `Completed(status)` from a delivery
  failure without parsing 255. The capability and harness-integration READMEs and ADR 0019 say so
  (collateral).
- **Contract versions.** The context a harness-integration and a git-credential stage receives
  changes shape, and so does `SetupInvocation`. Open question 4 proposes bumping both kinds'
  versions.

## Migration

Every row names what changes, the operation it uses and anything the HLA did not foresee, which is
an open question rather than new mechanism. Each migrated file joins the static legacy-import check
(`cli/tests/test_execution_import_boundary.py:24-60`) once it imports no legacy module.

| Caller                                                                                                                                   | Today                                                                                                                                                                                                                                                                                 | After                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Unforeseen                                                                   |
| ---------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------- |
| Artifact publication, `artifacts/publication.py:71-218`                                                                                  | `NativeFiles(runner, root=root)`: `fingerprint` of every path (`:107`), `read` of sections (`:121`), `publish` with an expected digest (`:157-159`, `:199-214`), `remove` (`:168`), `prune_empty_parents` (`:170`)                                                                    | Takes a `VMAccess`. `stat` for every path, `read(limit=16 MiB)` for sections, `write_if` (sections with `preserve_metadata=True`), `remove_if`, `prune_empty_parents`, all with `elevated=root`. Modes from `_mode` and the new `_directory_mode`. `ROOT_FILE_DIRECTORIES` and the root path rule move here from `native_files.py:193`, `:229-238`; `native_path` becomes `files.guest_path`                                                                                                                                                                                   | `stat` (open question 2)                                                     |
| Setup dispatch, `harness_setup/dispatch.py:41-56`, `:106-111`, `:208-217`                                                                | `destination_id` runs `cat /etc/machine-id` and `stat -Lc` through the runner; publication gets `call.runner`; `ROOT_FILE_DIRECTORIES` from `native_files` (`:30`)                                                                                                                    | `destination_id(vm, access, ...)` runs `Command(("cat", "/etc/machine-id"))` and `Command(("stat", "-Lc", "%d:%i:%u", "--", location))`, as the HLA keeps them (hla.md, release behaviors). Publication gets `call.access`                                                                                                                                                                                                                                                                                                                                                     | `SetupInvocation.access` (open question 3)                                   |
| Setup readiness, `harness_setup/readiness.py:68-70`, and its callers                                                                     | `destination_id(vm, runner)`; catches `SSHError` as unavailable                                                                                                                                                                                                                       | `require_setup_ready` and `_applicable_evidence` take a `VMAccess`; the execution errors read as unavailable. The session callers (`sessions/manager/_create.py:219`, `_create_roll.py:235`, `_lifecycle.py:758`) build it                                                                                                                                                                                                                                                                                                                                                     | `SetupReadiness.runner` (`setup.py:90`) has no reader at HEAD; it goes       |
| Setup lifecycle, `harness_setup/lifecycle.py:162-281`                                                                                    | Wraps the target in `SetupRunner` (`:179`, `:228`, `:269`)                                                                                                                                                                                                                            | Builds the invocation's `access` for the same identity                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | Whether `SetupRunner` and `runner` go now (open question 3)                  |
| Session artifacts, `artifacts/session.py:36-236`                                                                                         | `runner.run('printf "%s" "$HOME"')` (`:76`, `:220`); publication through the runner (`:152`, `:190`, `:228`)                                                                                                                                                                          | Each function takes a `VMAccess`. `HOME` is `Script('printf "%s" "$HOME"', Shell.SH)`, which sees the passwd home agw-launch sets (D4). The callers in `sessions/manager/` and `workspaces/manager/delete.py:190` build it                                                                                                                                                                                                                                                                                                                                                     | none                                                                         |
| Native probing, `artifacts/native/probe.py:370-437`                                                                                      | `require_python3` (`:384`), then `"$SHELL" -lic 'python3 -c <probe>'` with the request on stdin (`:402-410`)                                                                                                                                                                          | `PythonProgram(_PROBE, login=True, interactive=True, env=environment, stdin=Bytes(request))`. `require_python3` is deleted: every request proves python3 or is `VMNotReady` (D4). The `AGW_ARTIFACT_PROBE=` parse and the diagnostics redaction (`:411-437`) stay                                                                                                                                                                                                                                                                                                              | Runs `/usr/bin/python3`, not the login `PATH`'s (release notes)              |
| Native settings merge, `plugins/_harness_native/native.py:97-397`                                                                        | `NativeFiles(invocation.runner)`: `read`, `publish(expected=...)` that fails on a concurrent change (`:123`, `native_files.py:389-390`), `directory(root)` and `directory(root, create=True)` (`:238`, `:335`)                                                                        | `invocation.access.files`: `read` for the plan and the inventory checks, `update` for each publish with the plan's computation as `transform` (the initial skip decision kept, `:109-110`), `stat` for the root's existence, `ensure_directory(root, mode=0o700)`. User files 0600 with parents 0700; workspace files 0660 with parents 2770 and the group. A concurrent change is now retried, up to 3 times                                                                                                                                                                  | none                                                                         |
| Native CLI, `plugins/_harness_native/native_cli.py:97-324`                                                                               | Uses `NativeFiles`' staging directory as working directory and output files (`:122-144`, `:148-185`, `:308-315`); resolution runs `$SHELL -lc` inside a `python3 -c` program (`:29-57`); commands run as a `bash` script on a sensitive stdin with stderr to `/dev/null` (`:170-176`) | `NativeCLI` owns a guest scratch directory, made with `Command(("mktemp", "-d", "-t", "agentworks-native-XXXXXXXXXX"))` and removed with `Command(("rm", "-rf", "--", path))` on exit, as `NativeFiles.__enter__` and `__exit__` do (`native_files.py:265-291`). Resolution is `PythonProgram(login=True)` printing one marked JSON line. Each command is `Command((executable, *args), cwd=scratch, env=...)` with stdout `Capture(sensitive=True)` when structured, else `Discard`, and stderr `Discard`. `test -e` and `mkdir -p -m 700` become `Command`s (`:274`, `:310`) | Resolution is login only today (`:44`), not login and interactive as D5 says |
| Codex guidance, `plugins/codex/artifacts.py:61-66`                                                                                       | `NativeFiles(runner).read(override)`                                                                                                                                                                                                                                                  | `access.files.read(override)`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | none                                                                         |
| Claude prompt snapshot, `plugins/claude/artifacts.py:178-201`                                                                            | `"$SHELL" -lic 'claude --version'`; catches `SSHError`                                                                                                                                                                                                                                | `Script("claude --version", Shell.USER_DEFAULT, login=True, interactive=True, env=...)`; a failure to complete warns as an unknown version does today                                                                                                                                                                                                                                                                                                                                                                                                                          | none                                                                         |
| Harness setup facets: claude `:209-280`, codex `:465-548`, grok `:164-215` (`harness_integration.py`)                                    | `probe_native(invocation.runner, ...)`, `guidance_path(invocation.runner, ...)`                                                                                                                                                                                                       | `invocation.access`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | none                                                                         |
| Harness start stages: claude `:324-420`, `:495-537`; codex `:558-625`, `:707-830`, `:900-930`, `:1148-1340`; grok `:224-290`, `:363-396` | `ctx.admin_target()` or `agent_target()`; transcript, rollout, recorder and session probes as `"$SHELL" -lic '<inner>'`, reading exit codes                                                                                                                                           | `ctx.admin_access()` or `agent_access()`; each probe is `Script(inner, Shell.USER_DEFAULT, login=True, interactive=True)` with its exit-code protocol unchanged. `require_completed` raises the typed transport error where today's 255 became the probe's own `StateError`                                                                                                                                                                                                                                                                                                    | none                                                                         |
| Harness readiness, `capabilities/harness_integration/base.py:130-191`, `:462`, `:483-532`; `shell.py:154`; each `_probe_target`          | Reads the targets; `require_commands(commands, transport)`                                                                                                                                                                                                                            | Reads the accesses; `_probe_target(access)` and `require_commands(commands, access)`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | none                                                                         |
| Command checks, `command_checks.py:15-77`                                                                                                | A wrapper runs `"$SHELL" -lic` with a status line on descriptor 3 and `discard_output`, raising `SSHError` when indeterminate                                                                                                                                                         | `run_user_shell_command(command, access)` runs `Script(probe, Shell.USER_DEFAULT, login=True, interactive=True)`, where the probe runs `command` in a subshell and prints `agentworks-command-status:<n>` on stdout. stdout is captured and only that line is read, stderr is `Discard`. A missing or malformed line, or a non-`Completed` outcome, is indeterminate (an `ExternalError`, or the typed transport error)                                                                                                                                                        | Plan slice 6 lists this file, but all its callers are slice 5's (findings)   |
| Git-credential providers, `capabilities/git_credential/github.py:156-186`, `plugins/azure/azdo.py:138-166`                               | `ctx.admin_target() or ctx.agent_target()`; `SSHError` warns                                                                                                                                                                                                                          | `ctx.admin_access() or ctx.agent_access()`; an indeterminate check or a transport error warns, as today; `_GH_AUTH_CHECK` and `_AZ_AUTH_CHECK` unchanged                                                                                                                                                                                                                                                                                                                                                                                                                       | none                                                                         |
| `RunContext` construction sites (above)                                                                                                  | Pass targets                                                                                                                                                                                                                                                                          | Pass accesses beside them, or instead of them (open question 4)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | none                                                                         |

**`SetupInvocation`** (`capabilities/harness_integration/setup.py:18-33`) gains `access: VMAccess`.
After the rows above, every reader of `invocation.runner` has moved: the plugins' probes and
settings, `dispatch.py`'s `destination_id` and publication. Open question 3 asks whether `runner`
and `SetupRunner` go in this slice.

**Callers outside the plan's list.** `grok` and the `shell` integration are not named in plan slice
5, but `probe_native`, `require_commands` and the `RunContext` accessors change under them, so they
move with it. Neither needs anything new.

**After the slice**, `native_files.py` has no caller (the plan's definition of done), and neither do
`require_python3`, `NativeFiles` and `harness_setup/runner.py` if open question 3 is accepted. The
tests that exercise `_FILE_PROGRAM` directly (`cli/tests/test_native_file_boundaries.py`,
`test_native_path_failures.py`, `tests/artifacts/test_generated_sections.py:226-247`) move to
`file_program.py`, patching `files.program_source` where they patched `_FILE_PROGRAM`.

**The package guard.** The plugins stay on the static check because importing one loads the plugin
index (`cli/tests/test_execution_import_boundary.py:44-45`). The batch that migrates the last
harness plugin checks whether the index then imports no legacy module; if so, the plugins move to
the fresh-interpreter guard (D14), and if not, the module that still does is recorded there.

## Tests

None restates the implementation or our prose (`no-prose-policing-tests`).

**The plan's Tests box:**

| Plan item                                                                   | Test                                                                                                                                                                                                                                                   |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `write` applies mode and owner before the rename                            | The test holds the lock and starts a `write_if`; while it waits, its temporary file already has the final mode and group and the destination is untouched; released, the destination has them (`write` shares the path; its own test lands in slice 6) |
| `write_if` and `remove_if` detect a concurrent writer                       | Holding the lock, the test changes the destination after the operation staged; released, `FileConflict` and the destination keeps the test's bytes                                                                                                     |
| including a tree upload replacing the directory above the target            | Holding the lock, the test renames the target's grandparent aside and puts a new one in its place, as the swap does; released, `FileConflict`, and nothing is written into either tree                                                                 |
| JSON null survives an update                                                | `update` with the settings merge over an existing document holding `null`                                                                                                                                                                              |
| a malformed destination is refused                                          | `update` with a merge strategy over malformed JSON and TOML raises `ConfigError`; the file is byte-identical                                                                                                                                           |
| a symlinked destination is refused                                          | A link at the destination and at a parent, for every operation: `PathRefused(UNSUITABLE)`, and the link's target is untouched                                                                                                                          |
| cleanup touches only what the operation created                             | A conflict, a denied rename and an attribute-copy failure each leave no `.agentworks-tmp-*`; an unrelated pre-existing one survives; created parents remain                                                                                            |
| `preserve_metadata=True` keeps owner, group, mode and ACLs                  | The existing `tests/artifacts/test_generated_sections.py:193-265` cases, moved; owner and group under the sudo lane (below)                                                                                                                            |
| a sensitive stdin is absent from logs and exceptions (a conformance vector) | Vector F1                                                                                                                                                                                                                                              |

**The lock, against a real local filesystem.** `cli/tests/execution/test_file_program.py` runs the
program's `main` in subprocesses (`python3 -I -S -c` importing the module and calling
`main(argv, lock_path=..., lock_wait=...)`), so two operations and the test itself can contend, on a
temporary tree with `lock_path` in a sticky, world-writable temporary directory, on Linux CI
(skipped elsewhere: `O_PATH` is Linux's):

- two concurrent `write_if`s with the same expectation: exactly one publishes, the other conflicts;
- the lock file is created 0644 under umask 077, and a second identity's pre-existing lock file is
  opened, not recreated (under the sudo lane: `sudo -n -u nobody` creates it first, which
  `protected_regular` would otherwise turn into `EACCES`; the runner's own setting applies, and F6
  proves Debian's 2);
- a symlink planted at the lock path is `PathRefused(UNSUITABLE)` and is not followed;
- a held lock past `lock_wait` (0.2 s in the test) is `FileLockTimeout`, and nothing was renamed;
- `read` and `stat` succeed while another process holds the lock;
- a search-only (0711) ancestor works (I36);
- a `read` over `limit` is `TOO_LARGE` with nothing on stdout.

The sudo lane runs where `sudo -n true` succeeds. CI's Linux jobs already use passwordless sudo
(`.github/workflows/ci.yml:83-84`), so it is required there and skipped locally, as the S lane
treats `sshd`.

**Unit, every CI host:** the envelope round trip for `python` and `interactive`; the classifier and
error mapping for statuses 0 to 8 and an unknown one; `update` retries a conflict, stops after
three, and does not retry `FileLockTimeout` or `Unknown`; `transform` returning `None` or the same
bytes writes nothing; `format_record` prints `withheld` for each case; `stream` refuses `Bytes`;
`RunContext` construction is passive and refuses an access without its target (while both exist);
`guest_path`'s refusals; `files` on an agent access refuses `elevated=True` before dispatch.

**Caller tests stay green.** The native setup, publication and probe tests already run the real
guest program against local files on Linux (`cli/tests/native_setup_fixtures.py:20-80`,
`cli/tests/test_native_harness_setup.py:34`). They move from `LocalFixtureTransport` to a local
`VMAccess` built on the L lane (`cli/tests/execution/conformance/lanes.py`), promoted to
`cli/tests/vm_access_support.py`. The session and harness unit tests that use `_FakeTarget`
(`cli/tests/conftest.py:452`) move to `RecordingVMAccess` with outcomes keyed by the request's
script text.

### Conformance vectors

Lanes as `execution-lld.md` defines them; V runs on a Debian 12 and a Debian 13 guest.

| Vector                                                                                                                                                                                                                                                                                     | Lanes |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- |
| F1 A payload echoes a `Bytes(secret, sensitive=True)` stdin to both streams and exits 3 under `check=True`: the secret is in no `SSHLogger` file, `CommandFailed` message, hint or `repr`; the record says both streams were withheld. With `sensitive=False` the record holds the streams | L S V |
| F2 `Bytes` with every byte value, NUL included, reaches a `PythonProgram`'s `sys.stdin.buffer` exactly, at its first byte                                                                                                                                                                  | L S V |
| F3 `PythonProgram` arguments: empty, whitespace, shell characters and non-UTF-8 bytes arrive exactly; the source is absent from `/proc/<pid>/cmdline`                                                                                                                                      | L S V |
| F4 `PythonProgram` with `login`, `interactive` and both, under `sh`, `dash`, `bash` and `zsh`: a startup file's export is visible, the arguments are intact, nothing is prompted on stdout                                                                                                 | L V   |
| F5 `Script.interactive` under each shell: an interactive startup file's function is callable, and the sourced script's `exit N` is the status                                                                                                                                              | L V   |
| F6 The lock under `protected_regular=2`: created by the agent, then taken by the admin unelevated and elevated, and the reverse; a file program write by each in turn completes                                                                                                            | V     |
| F7 Elevated `write_if` and `read` of a root-owned file, and an agent's `write_if` refused when elevated                                                                                                                                                                                    | S V   |

F6 is the HLA's risk-table proof for the lock (hla.md, risks).

## Incident audit

The entry points this slice moves are `native_files.py`, `artifacts/publication.py`,
`artifacts/session.py`, `artifacts/native/probe.py`, `plugins/_harness_native/`, the harness
integrations' probes, `command_checks.py` and the two CLI-readiness providers. The audit read their
fix history (`git log` on each file), the native harness setup guide and the runtime git identities
SDD's plan (`docs/sdd/2026-08-28-runtime-git-identities/plan.md`, which records `b2f1dc992`'s
correction). It proposes these rows for the HLA's inventory, whose last row is I34 (`hla.md:850`):

| Row | Behavior                                                                                                                                                                                                                                                                                                                                                | Re-proved in |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| I35 | A native file is never read or written through a symbolic link, at the destination or any parent, and the refusal names the path and tells a link or wrong type from a denial (`a2eaba12e`; `native_files.py:20-40`, `:109-115`, `:199-215`)                                                                                                            | 5            |
| I36 | Traversal needs only search permission on each ancestor, so a search-only directory above a destination does not block setup (`93a4e1a6c`; `native_files.py:32`; `docs/guides/native-harness-setup.md:49`)                                                                                                                                              | 5            |
| I37 | A conditional publication checks the destination's digest and replaces it atomically from a sibling temporary file, so a concurrent edit fails it instead of being overwritten (`93a4e1a6c`; `native_files.py:140-184`; `tests/artifacts/test_generated_sections.py:109`)                                                                               | 5            |
| I38 | Generated-section updates and retirements keep the existing file's owner, group, mode, extended attributes and ACLs, drop an ACL the new file inherited from its directory, and leave the original untouched when the copy fails (`0decc2a75`, `3b3b9f69c`; `native_files.py:155-172`; `tests/artifacts/test_generated_sections.py:193-265`)            | 5            |
| I39 | Owned-file verification compares digest and mode on the guest without copying the content back (`4f4ab01ee`; `native_files.py:425-446`; `artifacts/publication.py:107`, `:137`)                                                                                                                                                                         | 5            |
| I40 | Elevated native file access stays inside the dedicated root directories for desired and recorded paths alike, and retirement keeps the boundary directories (`66f0edb54`; `native_files.py:193`, `:229-238`; `artifacts/publication.py:89-100`)                                                                                                         | 5            |
| I41 | Retirement prunes only empty parents up to the recorded package root, and keeps, with a warning, an empty root its parent will not release (`55ff7aee3`, `ad5a9733a`; `native_files.py:41-81`, `:408-423`)                                                                                                                                              | 5            |
| I42 | Native file contents and native CLI output stay out of the operation log (`native_files.py:334-354`; `plugins/_harness_native/native_cli.py:170-185`; `docs/guides/native-harness-setup.md:55-57`)                                                                                                                                                      | 5            |
| I43 | Native executables are resolved once from the actual user's login `PATH`, and an explicit `PATH` in the setup environment wins (`7f4a79b5c`; `plugins/_harness_native/native_cli.py:29-57`, `:118-144`)                                                                                                                                                 | 5            |
| I44 | Launch-target probes run in the user's login and interactive environment and read their status, so a probe that could not run never reads as absent, and a startup file's own exit is told from the probe's (`b2f1dc992`; `command_checks.py:15-77`; `plugins/claude/harness_integration.py:495-537`; `plugins/codex/harness_integration.py:1148-1236`) | 5            |
| I45 | Probe diagnostics have the setup environment's values removed, raw and shell-quoted, before they reach an error (`15b714480`, `5b51afcab`; `artifacts/native/probe.py:411-417`)                                                                                                                                                                         | 5            |
| I46 | Native discovery requests travel on stdin, so a large package needs no argument (`14a4b1336`; `artifacts/native/probe.py:402-410`)                                                                                                                                                                                                                      | 5            |
| I47 | A settings write that finds nothing to change leaves the file and its permissions alone (`plugins/_harness_native/native.py:122-123`; `docs/guides/native-harness-setup.md:52-53`)                                                                                                                                                                      | 5            |
| I48 | GitHub and Azure CLI readiness is checked read-only in the target user's shell, never blocks helper installation, and warns when the result is indeterminate (`f72884fdd`, `11e1e62ba`; `capabilities/git_credential/github.py:156-186`; `plugins/azure/azdo.py:138-166`)                                                                               | 5            |

**How each is re-proved.** I35 to I41 by the program and publication tests above, I38's owner and
group in the sudo lane and I38's ACLs live; I42 by F1 and a native setup test that reads its log;
I43 by the native CLI discovery tests (`cli/tests/test_native_cli_discovery.py`) on the local
access; I44 by F4, F5 and the probe protocol tests, with a startup file that exits 0; I45 and I46 by
the probe tests; I47 by the `update` unit test and the native setup tests; I48 by the provider
readiness tests. Each row also gets a live cell in the plan's live evidence.

`2841cb9fa` (python3 guaranteed for native helpers) needs no row: D10 already makes python3 a
provisioning requirement, and every request is `VMNotReady` without it.

## Collateral

- **ADR 0019** (the plan's box): `:86` names `ctx.admin_target()` and `ctx.agent_target()`; the
  status block gains an "Amended" paragraph naming `admin_access()` and `agent_access()`.
- **Plugin author documentation:** `capabilities/README.md:220-229` (the context's accessors);
  `capabilities/vm_platform/README.md:358`; `capabilities/harness_integration/README.md:458`,
  `:578`, `:671-678` (probes as `Script` with `login` and `interactive`, and
  `check_required_commands` on an access) and `:684-705` (the test fakes);
  `capabilities/git_credential/README.md` where it describes CLI readiness;
  `artifacts/README.md:426` (publication over `access.files`).
- **`cli/agentworks/execution/README.md`:** `Bytes`, the sinks, `PythonProgram`,
  `Script.interactive`, `files` and the lock, and `:197-198`'s list of guarded modules once
  `native_files.py` is gone; `cli/tests/execution/README.md`: the F vectors and the sudo lane.
- **`docs/guides/native-harness-setup.md`:** `:8-13` (python3 is now proven by every request, and
  `VMNotReady` names recover; there is no staging), `:46-53` (publication under the VM-wide lock; a
  concurrent change is retried before it fails), `:55-57` (unchanged promise).
- **`docs/guides/upgrading-to-0.20.md` and release notes:** the probe and the native CLI's
  resolution run `/usr/bin/python3`; settings conflicts are retried; a tree upload refuses a
  non-directory in its way (slice 6); the contract version bumps if open question 4 is accepted.
- **Contract versions**, if bumped: `capabilities/harness_integration/kinds.py:118`, `shell.py:74`,
  `plugins/claude/harness_integration.py:168`, `plugins/codex/harness_integration.py:409`,
  `plugins/grok/harness_integration.py:134`, `capabilities/git_credential/kinds.py:170`,
  `capabilities/git_credential/github.py:105`, `plugins/azure/azdo.py:95`, their tests, and each
  "version 7" or "version 3" mention of these kinds.
- **The HLA's inventory** gains rows I35 to I48 once this LLD is accepted.
- **Completions and sample config:** no command, option or setting changes.

## Live evidence

The plan's cells: harness setup and a session start with artifacts, on Remote Lima from macOS and on
one cloud from Windows. Each runs the V vectors F1 to F7 and:

- user and workspace setup with a settings mapping, then a second setup that changes nothing (I47);
- VM-scope artifacts elevated into `/etc/claude-code` (I40), and a generated section over a file
  with an ACL (I38);
- a session start that resumes from a transcript (I44), with an agent's access;
- `gh` readiness with and without a login (I48);
- F6 on Debian 12 and Debian 13; agw creates Debian 12 only on Proxmox (plan, slice 2), so that cell
  may join the carried-forward Debian 12 evidence.

## Delivery

Batches, each a reviewable unit that is green on its own, in the order they land in PR C:

1. **Requests.** `NoInput`, `Bytes`, `PythonProgram`, `Script.interactive`, `Capture` and `Discard`;
   the envelope, agw-launch and `oplog.py` changes; `stream`'s refusal; vectors F1 to F5 and the
   unit tests. No consumer yet, as PR B landed `root_script` before its platforms.
2. **Probes and delivery.** `RunContext.admin_access()` and `agent_access()` with every construction
   site; `command_checks.py`; the harness readiness and start probes; the claude prompt snapshot;
   the two CLI-readiness providers and `git_credentials`; the contract bumps.
3. **The file program and `files`.** `file_program.py`, `files.py`, the errors, the local filesystem
   and sudo-lane tests, vectors F6 and F7.
4. **Publication.** `SetupInvocation.access`, `dispatch.py`, `readiness.py`,
   `artifacts/publication.py` and `artifacts/session.py` with their callers, and the moved
   `_FILE_PROGRAM` tests.
5. **Native setup.** `native.py`, `native_cli.py`, `probe.py` and codex's `guidance_path`; deleting
   `native_files.py` and `require_python3` (and `SetupRunner`, per open question 3); the package
   guard check; the remaining collateral and the inventory rows.

## Open questions

1. **Output kept out of the log.** D13 withholds the streams only for a sensitive stdin, but `read`
   returns file content on stdout with no input, and the native CLI's output has the same promise
   (I42). Recommendation: `Capture(sensitive=True)`, withholding that one stream. Alternative: an
   empty `Bytes(b"", sensitive=True)`, which needs no new flag but says there is sensitive input
   when there is none; the runtime git identities effort replaced exactly that trick once
   (`docs/sdd/2026-08-28-runtime-git-identities/plan.md`, the `discard_output` correction).
2. **`stat`.** Publication compares digest and mode for every owned path without copying it back
   (I39, `publication.py:107`, `:137`), and an artifact package can total 64 MiB, so `read` would
   move it all on every setup; native setup also asks whether its config root exists
   (`native.py:238`). Recommendation: one `stat` operation, guest-hashed. Alternative: `read`
   returning the mode too, which moves every owned file's bytes each run.
3. **`SetupInvocation` and `SetupRunner`.** Plan slice 6 moves "the harness setup runners", but
   after this slice nothing reads `invocation.runner` (migration table), so it and `SetupRunner`
   would be dead. Recommendation: replace `runner` with `access` here and delete `SetupRunner`. Its
   one rule, that a plugin's local environment cannot override or add `AGENTWORKS_*` names
   (`harness_setup/runner.py:29-37`), then has no place to live; the plugins already compose their
   requests from `invocation.environment`, and a plugin that runs commands as the user can set any
   variable inside them, so the rule is no boundary. Alternative: keep `runner` beside `access`
   until slice 6, which leaves an unread field and a wrapper for one slice.
4. **The legacy accessors and contract versions.** D12 keeps `admin_target()` and `agent_target()`
   until slice 8, but every capability reader at HEAD moves in this slice (the harness integrations,
   `github.py`, `azdo.py`), and slices 5 to 8 ship together in PR C. Recommendation: delete them
   here, so each construction site switches rather than passes both, and bump the
   harness-integration contract to 8 and git-credential to 4 because their stages' context and
   `SetupInvocation` change shape. Alternative: keep them to slice 8 with the both-or-neither check,
   and bump at slice 8.
5. **`command_checks.py`'s slice.** Plan slice 6 lists it, but its three callers (harness readiness,
   `github.py`, `azdo.py`) are slice 5's. Recommendation: move it here. Alternative: a second copy
   on `VMAccess` until slice 6, which principle 8 rules out.
6. **Deleting `native_files.py`.** Plan slice 8 deletes it; after this slice it has no caller.
   Recommendation: delete it here with `require_python3`, since the same PR carries slice 8.
   Alternative: leave it to slice 8's sweep.
7. **The program's parameters.** Recommendation: one JSON argument, so every parameter is named and
   a path carries any byte. Alternative: positional arguments per operation, as `_FILE_PROGRAM`
   takes (`native_files.py:24-26`), which saves a decode and costs an argument order per operation.
8. **The lock wait.** Recommendation: 30 s, polled every 50 ms. Alternative: a shorter wait (5 s),
   which surfaces a stuck writer sooner but turns a slow guest's compare of a large file into a
   spurious timeout.

## Amendments

The HLA and the plan need these changes once the open questions are answered; this LLD does not edit
them.

- **Plan slice 5's implementation box** omits three items this design needs: `Script.interactive`
  and the `Capture` and `Discard` sinks, which D5 assigns to slice 5's consumers, and the `withheld`
  log keyword (open question 1).
- **Plan slice 6** lists `command_checks.py` and the harness setup runners, which this slice moves
  (open questions 3 and 5).
- **HLA D5** says the native CLI's login resolution sees the "login and interactive environment"
  today; it runs `$SHELL -lc`, login only (`plugins/_harness_native/native_cli.py:44`). The design
  keeps it login only.
- **HLA D6's table** has no operation for publication's digest-and-mode check (open question 2).
- **HLA D12 and slice 8** keep the legacy accessors past their last reader (open question 4).
