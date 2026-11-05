# Transport Take 2: Files Low-Level Design (Slice 5)

- Status: Revised after the private review; open questions answered by the lead on 2026-11-05.
- Date: 2026-11-05
- Architecture: [hla.md](./hla.md) D5 (`PythonProgram`, input and sinks), D6 (files), D12 (delivery
  to consumers), D13 (errors and the operation log), D14 (the guards); requirements: R4, R5, R9,
  R10, R11
- Plan: [plan.md](./plan.md), slice 5, delivered in PR C
- Source baseline: `e645220ad` (the PR B branch head). Every `path:line` is at that commit. Paths
  are under `cli/agentworks/` unless they start with `cli/` or `docs/`.

## Scope

This LLD settles slice 5: the file operations built on `run`, the request vocabulary they and the
probes need, the `RunContext` accessors, and the migration of every caller onto them.

**Operator direction applied.** "Please, please, please don't take on a bunch of extra complexity"
(FRD, 2026-11-01). Every operation and field below has a caller in this slice. One lock rule covers
every mutation. Hostile writers stay with the permissions effort (R5).

**What exists at HEAD.** No `PythonProgram`, `Bytes`, sink type or `Script.interactive` exists
(`execution/requests.py:48-124`). `VMAccess.run` closes ssh's stdin right after the request and
captures each stream up to the pump's 1 MiB (`execution/access.py:94-114`, `execution/pump.py:37`,
`:93-104`). The file program is `native_files.py`'s `_FILE_PROGRAM` (`native_files.py:22-189`), sent
as `python3 -c` argv with a scp staging directory (`:258-298`, `:338`, `:371`).

**Out of this slice.** Every slice 5 write is conditional. Slice 6 adds `write` and `upload_tree`,
and slice 7 the unconditional `remove`, each designed with its first caller on this program and
lock. `FromFile`, `ToFile` and the `observation` option stay slice 7.

## Module layout

| Module                                                            | Contents                                                                                                                                                                              |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `execution/requests.py`                                           | `NoInput`, `Bytes`, `stdin` and `log_streams` on `BoundedRequestOptions`, `PythonProgram`, `Script.interactive`, `CaptureOutput`, `DiscardOutput`; `RunRequest` gains `PythonProgram` |
| `execution/envelope.py`                                           | The `python` kind, the `interactive` flag, caller input after the envelope frame                                                                                                      |
| `execution/agw_launch.py`                                         | Runs a `python` payload and both interactive startups                                                                                                                                 |
| `execution/access.py`                                             | `run(request, *, stdout=, stderr=, check=)`, the `files` property, `stream`'s refusal of input                                                                                        |
| `execution/oplog.py`                                              | The `PythonProgram` shape; `log_streams=False` withholds both streams                                                                                                                 |
| `execution/file_program.py` (new)                                 | The guest file program: plain syntax, standard library only, importable                                                                                                               |
| `execution/files.py` (new)                                        | `Files`, `guest_path`, the result types, `update`, `program_source()`                                                                                                                 |
| `execution/errors.py`                                             | `FileConflict`, `PathRefused`, `FileLockTimeout`                                                                                                                                      |
| `capabilities/base.py`                                            | `RunContext.admin_access()` and `agent_access()`; `admin_target()` and `agent_target()` deleted                                                                                       |
| `capabilities/harness_integration/setup.py`                       | `SetupInvocation.access` and `request_environment()`; `runner` deleted; `SetupReadiness.runner` deleted                                                                               |
| `command_checks.py`                                               | On `VMAccess`                                                                                                                                                                         |
| `artifacts/publication.py`                                        | Gains `ROOT_FILE_DIRECTORIES` and the root path rule from `native_files.py:193`, `:229-238`                                                                                           |
| `native_files.py`, `harness_setup/runner.py`                      | Deleted, after their incident rows are re-proved (R10)                                                                                                                                |
| `cli/tests/execution/test_file_program.py`, `test_files.py` (new) | The program against a real local filesystem, and the API over the L lane                                                                                                              |
| `cli/tests/vm_access_support.py`                                  | `RecordingVMAccess` handles `files`; a local access over the L lane for caller tests                                                                                                  |

`file_program.py` is a separate payload from agw-launch, as D6 requires: `files.program_source()`
reads it from the installed package as `envelope.launcher_source()` reads `agw_launch.py`, and only
a file operation sends it. It has no import-time effect. It defines
`main(argv, *, lock_path=LOCK_PATH, lock_wait=LOCK_WAIT_SECONDS) -> int` and ends with
`if __name__ == "__main__": sys.exit(main(sys.argv[1:]))`, so tests call `main` with a temporary
lock, and the package guard can import it on any workstation (`fcntl`, `pwd` and `grp` are imported
inside `main`, as agw-launch keeps `pwd` out of module level).

## Requests this slice adds

The slice 2 vocabulary (`execution-lld.md`, "Requests and results") left these to their first
consumer. Every one has one here.

### Input

`BoundedRequestOptions` (so `Command`, `Script` and `PythonProgram`) gains
`stdin: NoInput | Bytes = NoInput()`. `LoginShell` has none, so incident row I4's refusal of input
with a terminal stays structural.

- **`Bytes(data: bytes)`**, `data` `repr=False`. The client writes `data` after the envelope frame,
  as D3's third part, then closes ssh's stdin. `PipeSession` already takes the request bytes and an
  optional stream (`pump.py:211-237`); `Bytes` is appended to the request bytes, so the pump needs
  no change. Nothing bounds it but memory (D3).
- **`stream`** takes the operator's stdin as its own argument (`access.py:116`). `VMAccess` is a
  boundary once plugins hold it through `RunContext`, so `stream` raises `ValidationError` before
  dispatch for a request whose `stdin` is not `NoInput`, beside the checks `envelope.encode` already
  makes on every request a plugin builds.

### `log_streams`

`BoundedRequestOptions` gains `log_streams: bool = True`. `format_record` reads it from the request:
with `False` it writes `streams: not logged` in place of both streams. Every consumer that needs it
wants both streams withheld, so there is no per-stream choice. Its consumers are the file program's
`read` and `stat` (their stdout carries file content or its digest) and the native CLI (I42).

**Stdin never reaches a record or an exception, whatever the flag.** `format_record` formats the
request's shape from `argv`, the shell and flags, `cwd`, environment names and the deadline
(`execution/oplog.py:40-52`), and never touches `stdin`; `Bytes.data` is `repr=False`, so a
request's `repr` in a traceback omits it; and every execution error is built from the VM name and
the `Result` (`execution/errors.py`), which holds the payload's streams and never the input. What
the flag governs is the payload's own output: a payload that echoes its stdin puts it in its
streams, which `log_streams=False` keeps out. Vector F1 proves both halves.

### Buffered sinks

`run(request, *, stdout=CaptureOutput(), stderr=CaptureOutput(), check=False)`, where each sink is a
`CaptureOutput` or a `DiscardOutput`. The names keep clear of `pump.Capture`, the sink callable they
map to.

- **`CaptureOutput(limit: int = 1_048_576)`** keeps up to `limit` bytes; more is
  `Unknown(OUTPUT_LIMIT)`, as today. The limit is the single bound on a read: callers whose output
  can exceed 1 MiB set it per call (below).
- **`DiscardOutput()`** keeps nothing; the stream still runs through its filter, so the markers and
  the trailer are read. Its `Result` field is empty. Consumers: `command_checks.py` and the native
  CLI (D5's table).

### `PythonProgram`

`PythonProgram(source: str, args: tuple[str, ...] = (), login: bool = False, interactive: bool = False)`
with the bounded options. `source` and `args` are `repr=False`, as `Script.source` and
`Command.argv` are.

- **Envelope.** `kind: "python"` with `source`, `args`, `login` and `interactive`. `envelope.encode`
  validates `args` as it does argv (no NUL, each element at most 131,071 bytes once encoded) and
  `source` as script text.
- **agw-launch** writes the source to the same sealed memfd a script uses (`execution-lld.md`,
  agw-launch step 5) and runs, with `N` the memfd's descriptor:
  - neither flag: `/usr/bin/python3 -I -S /dev/fd/N *args`;
  - either flag: `<passwd shell> [-l] [-i] -c '<line>' /dev/fd/N *args`, with `<line>` the constant
    `exec 9<"$0"; exec /usr/bin/python3 -I -S /dev/fd/9 "$@"`. The shell reopens the memfd as
    descriptor 9 itself, so whatever startup does to inherited descriptors, the program's source is
    open when python3 starts. The `-c` string is constant and the arguments are positional, so
    nothing is quoted (D5).
- **Measured** on the baseline host (Debian 12.15: bash 5.2.15, dash 0.5.12 as `/bin/sh` and `dash`,
  zsh 5.9, python3 3.11.2), with the program in an inherited memfd and each of `-c` alone, `-l`,
  `-i` and `-l -i`, under the host's own startup files (oh-my-zsh, mise) and under an empty home:
  - `exec /usr/bin/python3 -I -S "$0" "$@"` fails under `bash -l -i` (python3 cannot open
    `/dev/fd/N`: the inherited descriptor is closed on exec) and works in the other 15 cases;
  - `exec 9<"$0" && exec ...` works in all 16 with an empty home, but fails under `zsh -i` and
    `zsh -l -i` with the host's startup files: zsh leaves `$?` at the startup's last status after an
    `exec` that only redirects, so `&&` never runs the program;
  - `exec 9<"$0"; exec ...` works in all 16 under both homes, with the program seeing `a` and `b c`
    as its arguments and the shells' job-control notices on stderr only.
- **Vector F4** re-measures this on the L lane and every platform's V lane before the probe ships.
- **Why `/usr/bin/python3`.** The probe ran `python3` from the login `PATH`
  (`artifacts/native/probe.py:402-410`), so a mise-managed Python could serve it. It now always gets
  the system interpreter that agw-launch already proves. A behavior change for release notes.

### `Script.interactive`

`Script` gains `interactive: bool = False`. With it, agw-launch runs
`<interpreter> [-l] -i -c '. "$0"' /dev/fd/N`, sourcing the memfd from an interactive shell. Slice 2
measured that `dash -i FILE` prints a prompt per line it reads (`execution-lld.md`, "Left to the
first consumer"); `-c` reads no commands from a file or stdin. The shell itself opens the path, so
close-on-exec plays no part. Measured on the baseline host under all four shells, with `-i` and
`-l -i`, under both homes above: a sourced `exit 7` is the shell's status. The consumers are the
launch-target probes, which run `"$SHELL" -lic '<inner>'` today
(`plugins/claude/harness_integration.py:525`, `plugins/codex/harness_integration.py:1166`, `:1224`,
`:1321`, `plugins/grok/harness_integration.py:384`, `plugins/claude/artifacts.py:186-192`,
`command_checks.py:27-49`).

**Operation log shapes.** `python program (login, interactive), args <shlex.join(args)>` and
`script (user_default, login, interactive)`, then the common parts. Neither source is written.

## The file program

Every operation is one `run` of one `PythonProgram`: `source` is `program_source()`, `args` is
`(op, params)` with `params` one JSON object (`ensure_ascii=True`, compact), and `elevated` is the
caller's flag. JSON keeps every parameter named. Parameters are paths, modes, a group name and
digests; content is never an argument. Paths are text: `guest_path` refuses a path that is not
printable (`native_files.py:218-226`), which is the intended stance.

| Op                    | Params                                                                           | stdin   | stdout                       |
| --------------------- | -------------------------------------------------------------------------------- | ------- | ---------------------------- |
| `read`                | `path`                                                                           | none    | the file's bytes             |
| `stat`                | `path`                                                                           | none    | `{"kind", "mode", "digest"}` |
| `write_if`            | `path`, `expected`, `mode`, `group`, `parent_mode`, `preserve`, `size`, `sha256` | content | `{}`                         |
| `remove_if`           | `path`, `expected`                                                               | none    | `{}`                         |
| `ensure_directory`    | `path`, `mode`                                                                   | none    | `{}`                         |
| `prune_empty_parents` | `path`, `root`                                                                   | none    | `{}`                         |

- **stdout** is the file's bytes for `read` and one JSON line otherwise. The program prints nothing
  before it, and agw-launch's filter drops everything before the start marker.
- **stderr** carries only a Python traceback, for status 1.
- **Statuses.** The program's own exit status is the answer:

| Status | Meaning                                                                                     | Workstation result         |
| ------ | ------------------------------------------------------------------------------------------- | -------------------------- |
| 0      | Done                                                                                        | the parsed result          |
| 1      | Uncaught exception, or `128 + n` after signal `n` (below)                                   | `CommandFailed` (existing) |
| 2      | Conflict: the destination does not hold what `expected` says                                | `FileConflict`             |
| 3      | `prune_empty_parents` kept an empty root its parent will not release (`native_files.py:73`) | `Pruned.ROOT_RETAINED`     |
| 4      | A link or wrong type at the destination or a parent (`native_files.py:36-37`)               | `PathRefused(UNSUITABLE)`  |
| 5      | `EACCES`, `EPERM` or `EROFS` (`native_files.py:38-39`)                                      | `PathRefused(DENIED)`      |
| 6      | `write_if`'s content did not arrive as sent: its size or SHA-256 differs from the params    | `CommandFailed`            |
| 7      | The lock was not acquired within its wait                                                   | `FileLockTimeout`          |
| 8      | `read` of an absent file                                                                    | `None`                     |

Statuses 2 to 5 keep `native_files.py`'s numbers, so `a2eaba12e`'s classification carries over. Any
outcome other than `Completed` raises through `Result.require_completed` first
(`execution/results.py:84-96`), so transport failures keep their D13 types; a `read` over its
capture limit is `ExecutionIncomplete(Unknown(OUTPUT_LIMIT))`, which names the limit.

### Walking a path

`files.guest_path(path)` is the one validator, at the workstation boundary: absolute, printable, and
no empty, `.` or `..` component, as `native_path` checks today. It raises `StateError` as
`native_path` does, since publication uses it on plugin output and recorded paths
(`artifacts/publication.py:59-67`, `:89-100`). The program receives only validated paths.

- **Parents.** The program opens `/` with `O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC` and each
  parent component relative to the last, with the same flags (`native_files.py:32`, `:82-99`). A
  symlink component fails with `ELOOP` or `ENOTDIR` (status 4). `O_PATH` needs only search
  permission, so a search-only ancestor works (I36).
- **A missing parent.** `read`, `stat` and `remove_if` report the target absent. `write_if` and
  `ensure_directory` create it: `mkdir` relative to the parent descriptor with mode 0700, an
  `O_RDONLY | O_DIRECTORY | O_NOFOLLOW` open of what was made, `fchown(-1, gid)` when `write_if`
  names a group, then `fchmod(parent_mode)`, or `ensure_directory`'s `mode`, so the umask plays no
  part (`native_files.py:91-97`).
- **The destination.** `stat` uses `os.stat(name, dir_fd=parent, follow_symlinks=False)`: a symlink
  or any type but a directory or a regular file is status 4, a directory is reported as one, and
  only a regular file is opened. `read`, `stat`'s hash and every compare open a regular file with
  `O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC` (`native_files.py:109`) and `fstat` it: a link
  (`ELOOP`) or anything not regular is status 4. `O_NONBLOCK` keeps a FIFO from blocking the open.
  Since `write_if` and `remove_if` compare under the lock before they rename or unlink, the compare
  is also their type check: neither ever replaces or removes a link.

**Left to the permissions effort** (R5): a writer that does not take the lock can rename or replace
a parent directory, not a link, or change the destination, between the walk and the rename; it can
create the lock file with a mode others cannot open, put something other than a file there, or hold
the lock to stall them (D6); a hard link it owns into a destination directory is followed by `read`
(Debian's `fs.protected_hardlinks` limits it to files it can already write); and an elevated
operation walks directories an agent may be able to write. The elevated allowlist
(`ROOT_FILE_DIRECTORIES`) is caller policy in `artifacts/publication.py`, not enforcement in the
transport (FRD scope: no core file-mutation allowlist).

### The lock

One file, `LOCK_PATH = "/run/lock/agentworks-files.lock"`, for every mutating operation of every
identity (D6). The program acquires it in this order:

1. `open(LOCK_PATH, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)`.
2. On `ENOENT`:
   `open(LOCK_PATH, O_RDONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, 0o644)`, then
   `fchmod(fd, 0o644)` whatever the umask.
3. On `EEXIST` from step 2 (it lost the race): step 1 once more.
4. A symlink at the path (`ELOOP`) is status 4, and a denial status 5, each naming the lock file.
   Whatever else is there is opened and locked like the file; `O_NONBLOCK` keeps a FIFO from
   blocking the open, and only a hostile writer could have put one there.
5. `flock(fd, LOCK_EX | LOCK_NB)`, retried every 50 ms until `LOCK_WAIT_SECONDS` (30) have passed,
   then status 7. Closing the descriptor, or the process ending, releases it.

The order is D6's: under `fs.protected_regular=2` an `O_CREAT` open of another identity's file in
the sticky `/run/lock` fails with `EACCES` even for root (measured on Debian 12 and 13, integration
delta on #856), and with `O_EXCL` the kernel reports `EEXIST` before it applies that rule, so step 2
fails only when the file exists. `flock` needs no write access, so a read-only descriptor serves
every identity. On the baseline host `/run/lock` is `drwxrwxrwt root root` and `protected_regular`
is 2. Vector F6 runs on every platform's V lane, since `/run/lock` comes from each image's systemd
(a live cell, not a contract row).

**One rule: a mutating operation holds the lock from its walk to its last system call.**

- `write_if`: read and verify the content (below), then take the lock, walk and create parents,
  create the temporary file, write it, set its group and mode, compare the destination, copy
  metadata when asked, `fsync` it, rename, `fsync` the directory, exit.
- `remove_if`, `ensure_directory` and `prune_empty_parents`: take the lock, walk, act, exit.
- `read` and `stat` take no lock. A rename replaces the inode atomically, so a reader sees the old
  file or the new one whole.

Because every cooperating writer walks under the lock, no writer can stage in a directory another
then replaces, and nothing has to be re-checked before the rename.

**The wait.** The longest hold is writing the content already in memory (up to the 64 MiB artifact
bound), a compare of the existing file (SHA-256 of 16 MiB takes tens of milliseconds) and an
attribute copy, so 30 s expires only when a writer is stuck or hostile.

### Content and its verification

`write_if` reads stdin to end of file into memory before it creates anything, as today's program
reads its staged file whole (`native_files.py:147-148`). It then compares the byte count and the
SHA-256 with the `size` and `sha256` parameters. On a mismatch it exits with status 6 having created
nothing: a connection that drops mid-transfer gives the program a short stdin and end of file, and
this is what keeps a truncated body from being published. Only then does it take the lock.

### Mode and ownership

Under the lock, `write_if`:

1. Creates `.agentworks-tmp-<24 hex>` relative to the parent descriptor with
   `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC`, mode 0600, and writes the content.
2. `fchown(-1, gid)` when `group` names one (`native_files.py:146`), then `fchmod(mode)`; chown
   first, because it clears set-id bits.
3. Compares the destination with `expected` (a digest, or `None` for absent): a mismatch is
   status 2.
4. With `preserve` and an existing destination: `fchown` to its owner and group, `fchmod` to its
   mode, then removes every extended attribute the temporary file has that the destination lacks (an
   ACL inherited from a default directory ACL) and sets every one the destination has, ACLs
   included. This is `native_files.py:155-172`'s order, so an ACL's mask survives the `fchmod`. A
   failure exits before the rename, leaving the original untouched
   (`cli/tests/artifacts/test_generated_sections.py:226-247`).
5. `fsync`s the temporary file, after the attribute copy so the copy is durable too.
6. Renames it over the destination, then `fsync`s the directory through an
   `O_RDONLY | O_DIRECTORY | O_NOFOLLOW` open of `.` relative to the parent (an `O_PATH` descriptor
   cannot be `fsync`ed; `native_files.py:174-179`).

- **Unelevated**, `group` must be one the identity belongs to; the kernel refuses anything else
  (status 5). Workspace settings and artifacts use the workspace group this way
  (`plugins/_harness_native/native.py:145`, `harness_setup/dispatch.py:215`).
- **Elevated** placement is the request's `elevated` flag on each call (D6), so new files and
  directories are root's. No caller writes a copy, chmod or sudo wrapper. An agent-bound access
  refuses an elevated operation before dispatch with `PermissionDenied` (D12), because `run` does.
- **Agent writes** use the agent's `VMAccess`, so the kernel applies that account's permissions.
- **Modes are the caller's.** `files.py` has no mode policy. `publication.py` keeps `_mode`
  (`publication.py:221-224`) and gains `_directory_mode` (0755 elevated, 2770 with a group, else
  0700, from `native_files.py:91-97`); native settings pass 0600 with parents 0700, or 0660 with
  parents 2770 and the workspace group (`docs/guides/native-harness-setup.md:51-53`).

### Cleanup

- **`write_if`.** A `finally` unlinks the temporary name, ignoring `ENOENT` after a successful
  rename (`native_files.py:180-184`). Directories it created stay: R5 asks that cleanup touch only
  what the operation created, which keeping them satisfies, and retirement has `prune_empty_parents`
  for them.
- **What a dropped connection does.** Under `ssh -T` there is no pseudo-terminal, so no hangup
  reaches the payload when the connection drops; sshd closes the channel's pipes and the payload
  runs on (the predecessors measured guest trees outliving their request, HLA D5 "Deadlines"). The
  design relies on no signal. A drop during the transfer is a short stdin, and status 6 creates
  nothing. A drop after it lets the operation finish under the lock; its final stdout write may then
  fail, after the rename. If a `SIGHUP` or `SIGTERM` does arrive, the program's handlers raise
  `SystemExit(128 + signal)`, so the `finally` runs and the status is nonzero. Only `SIGKILL`
  between the create and the rename can leave a `.agentworks-tmp-*` file, which no operation reads.
- **An ambiguous outcome.** A write whose `run` ends `Unknown` may or may not have renamed. `files`
  raises `ExecutionIncomplete` and nothing retries it, `update` included (R4).
- **`remove_if`, `ensure_directory`, `prune_empty_parents`** create nothing to clean up.

## The workstation API

`VMAccess.files` returns `Files(self)`, building nothing else (R9):

```python
def read(path: str, *, limit: int = 1_048_576, elevated: bool = False) -> FileContent | None
def stat(path: str, *, elevated: bool = False) -> PathState
def write_if(path: str, data: bytes, *, expected: str | None, mode: int, parent_mode: int,
             group: str | None = None, preserve_metadata: bool = False,
             elevated: bool = False) -> None
def update(path: str, transform: Callable[[bytes | None], bytes | None], *, mode: int,
           parent_mode: int, group: str | None = None, preserve_metadata: bool = False,
           limit: int = 1_048_576, elevated: bool = False) -> bool
def remove_if(path: str, *, expected: str, elevated: bool = False) -> None
def ensure_directory(path: str, *, mode: int, elevated: bool = False) -> None
def prune_empty_parents(path: str, *, root: str, elevated: bool = False) -> Pruned
```

- **`read`** returns `FileContent(data, digest)` (`data` `repr=False`) or `None` when the file or a
  parent is absent, with `log_streams=False` and stdout `CaptureOutput(limit)`.
- **`stat`** returns `Absent()`, `Directory()` or `RegularFile(digest, mode)`, with `mode` the
  permission bits, also with `log_streams=False`. It ports today's `fingerprint` and `directory`
  (`native_files.py:307-332`, `:425-446`).
- **`write_if`** publishes only while the destination still has digest `expected`, or is still
  absent for `None`. Content travels as `Bytes(data)`, with `size` and `sha256` in the params.
- **`remove_if`** removes a regular file whose digest is `expected`. An absent file is a conflict,
  as `native_files.py:140-141` treats it, because the caller observed it present.
- **`ensure_directory`** creates the directory and its missing parents with `mode`, and never
  changes an existing directory.
- **`prune_empty_parents`** removes the empty parents of `path` from the deepest up to and including
  `root`, which must be a proper ancestor (refused before dispatch, `native_files.py:411-412`), and
  stops at the first non-empty one (`native_files.py:41-81`). When removing `root` itself is denied,
  the program opens it again without following links, checks it is the directory it walked by
  `(st_dev, st_ino)` and that it is empty, and only then reports status 3, `Pruned.ROOT_RETAINED`;
  otherwise the denial is an error (`cd735e8b6`, `native_files.py:64-76`). The caller warns, as
  `native_files.py:415-418` does.

**Errors**, in `execution/errors.py`. Each names the VM, the operation and the path, never content
or a stream (D13).

| Error                  | Kind         | Raised for                                                                |
| ---------------------- | ------------ | ------------------------------------------------------------------------- |
| `FileConflict`         | `StateError` | Status 2, and `update` after its last attempt                             |
| `PathRefused(reason)`  | `StateError` | `UNSUITABLE` (status 4) or `DENIED` (5)                                   |
| `FileLockTimeout`      | `StateError` | Status 7; the hint says another Agentworks operation is writing on the VM |
| `CommandFailed`        | existing     | Status 1, 6, or any status outside the table                              |
| D13's transport errors | existing     | Any completion other than `Completed`, through `require_completed`        |

`PathRefused(UNSUITABLE)` carries today's hint (`native_files.py:204-208`): Agentworks never reads
or writes through a link, and dotfile installers are the usual cause. The per-surface subject
today's messages add (`native_files.py:199-215`) goes, since the path already says which file failed
and no caller branches on it; its test (`cli/tests/test_native_path_failures.py:46`) goes with it.

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
- **Unchanged content writes nothing**, so the file keeps its permissions (I47).
- **Only `FileConflict` retries.** `FileLockTimeout`, refusals and every transport outcome
  propagate: a lock held for 30 s will not clear in milliseconds, and an `Unknown` write must not be
  dispatched again (R4).
- **The merge stays on the workstation** (D6). `native.py`'s transform calls
  `capabilities/harness_integration/settings.py`, which keeps JSON null as a value and rejects
  malformed input (`settings.py:78-104`, `:125-144`), so R5's merge rules hold by construction.

### Digests and size limits

- **SHA-256**, lowercase hex: the form `OwnedArtifactFile.sha256` records and `publication.py:184`
  computes, so recorded ownership needs no migration.
- **Where.** `read`'s digest and `write_if`'s `sha256` are computed on the workstation; `stat`'s and
  every compare are computed by the program, the compares under the lock.
- **The capture limit is the only read bound.** The default is the pump's 1 MiB. Publication's
  generated-section reads pass `DEFAULT_CAPTURE_LIMITS.total_bytes` (64 MiB,
  `package_sources.py:27-36`), since a section file holds artifact content up to that bound around
  the operator's own; native settings and Codex's override file keep the default.
- **Writes** are bounded by memory on both ends (D3).
- **`stat`** transfers no content, which is why publication uses it.

## Capability delivery: `admin_access()` and `agent_access()`

`RunContext.__init__` takes `admin_access: VMAccess | None = None` and
`agent_access: VMAccess | None = None` in place of `admin_target` and `agent_target`, stored
privately as they were (`capabilities/base.py:202-224`), with pass-through accessors in place of
`admin_target()` and `agent_target()` (`:226-234`). D12 keeps the targets "until the last capability
consumer moves", and every capability reader moves in this slice, so they are deleted in the batch
that moves the last one.

- **Lifecycle.** An access is present exactly where a target was, at the same timing
  (`capabilities/base.py:166-176`): preflight sees the identities that already exist, runup the
  current ones. Each construction site builds it from the row it already holds with
  `execution.vm_access(vm, config, Admin() or Agent(name, linux_user), log=logger.record)`.
  Construction copies names and paths and nothing else, so a context still connects to nothing,
  starts no VM and resolves no secret (R9).
- **Construction sites**, each switching from target to access: `vms/manager/lifecycle.py:317-330`
  and `vms/manager/reinit.py:212-222` (their `scoped_ctx`), `agents/manager/lifecycle.py:216-228`
  and `:660-670`, `sessions/manager/_create.py:200-208`, `sessions/manager/_create_roll.py:62-70`,
  `:222-227` and `:330-335`, `sessions/manager/_lifecycle.py:744-751` and `:880-885`, and
  `git_credentials/__init__.py`'s `ScopedContextFactory` (`:39-48`) and `CredentialRequest.context`
  (`:63-77`). These are slice 6 and 7 code still on the legacy stack; building a `VMAccess` there is
  legacy code calling new code, which R10 allows.
- **Signature changes in slice 6 code**, made before the targets go:
  - `apply_vm_setup` (`harness_setup/lifecycle.py:162-172`) gains `config: Config` and
    `logger: SSHLogger`, as its two siblings already take (`:203`, `:242`), to build the
    invocation's access; its caller passes them (`vms/initializer/driver.py:205-218`).
  - `materialize_credential_state` (`git_credentials/__init__.py:107-142`) takes `access: VMAccess`
    in place of `target`, and `configure_user_git_credentials` (`:145-165`) gains `access: VMAccess`
    beside the `target` its reconcile still uses until slice 6. Its callers pass the admin's and the
    agent's access (`vms/initializer/driver.py:544`, `agents/initializer.py:283`).
- **What plugin authors see.** `ctx.admin_access()` and `ctx.agent_access()` return a `VMAccess`
  bound to that identity, or `None`. They build requests from `agentworks.execution.requests`, call
  `run`, and use `access.files`. The agent's access refuses `elevated=True` before dispatch with
  `PermissionDenied`. Results are typed, so a probe tells `Completed(status)` from a delivery
  failure without parsing 255.
- **Contract versions.** Harness-integration goes from 7 to 8 and git-credential from 3 to 4, once
  each for PR C. Every plugin-facing surface that carries a legacy transport changes in this slice:
  `RunContext`'s targets, `SetupInvocation.runner`, `SetupReadiness.runner`, and the `_probe_target`
  and `require_commands` parameters (the only `Transport` mentions under `capabilities/`,
  `capabilities/harness_integration/base.py:50`, `setup.py:15`, `capabilities/base.py:56`). Slices 6
  to 8 change core callers only (initializers, `git_credentials/reconcile.py`, sessions, backup) and
  no type a plugin implements or receives, so they need no second bump. The vm-platform kind stays
  at 2.

## `SetupInvocation` and the setup environment

`SetupInvocation` (`capabilities/harness_integration/setup.py:18-33`) gains `access: VMAccess` and
loses `runner` once nothing reads it; `SetupReadiness.runner` (`:90`), which no plugin reads, goes
in the batch that moves `readiness.py`. `harness_setup/runner.py` is then deleted.

- **The rule that survives.** `SetupRunner._env` (`harness_setup/runner.py:29-37`) lets a plugin's
  own variables override the setup environment except for `AGENTWORKS_*` names, which keep core's
  value or are dropped (`bcca20a86`; `docs/guides/harness-facets.md:189-192`). It becomes
  `SetupInvocation.request_environment(local: Mapping[str, str] | None = None) -> dict[str, str]`,
  the same function moved, which plan slice 6's "environment rules become request environment" asks
  for.
- **What disappears.** `SetupRunner.run` injected the setup environment into every command it ran
  (`runner.py:54`), the file program and staging commands included. Now each request passes `env=`
  itself: the probe, the native CLI and its resolution pass `invocation.request_environment(...)`;
  file operations pass none, since the program reads no environment.
- **Elevation.** `SetupRunner` carried the environment across `sudo` with `--preserve-env` and a
  `bash -c` wrapper (`runner.py:55-57`, `5797e5110`). The envelope makes that hold by construction:
  agw-launch applies the request's `env` after `sudo -n` has started it (D4), so nothing has to
  survive sudo. Vector F8 re-proves it.

## Migration

Every row names what changes and the operation it uses. Each migrated file joins the static
legacy-import check (`cli/tests/test_execution_import_boundary.py:24-60`) once it imports no legacy
module. Today's timeouts become `deadline=` with the same value; the last column says what
`Unknown(DEADLINE)` becomes.

| Caller                                                                                                                                   | Today                                                                                                                                                                                                                                                                                                                              | After                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | On `DEADLINE`                                                       |
| ---------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------- |
| Artifact publication, `artifacts/publication.py:71-218`                                                                                  | `NativeFiles(runner, root=root)`: `fingerprint` of every path (`:107`), `read` of sections (`:121`), `publish` with an expected digest (`:157-159`, `:199-214`), `remove` (`:168`), `prune_empty_parents` (`:170`)                                                                                                                 | Takes a `VMAccess`. `stat` for every path, before any write; a `Directory` where a file is expected raises `PathRefused(UNSUITABLE)` naming it, as today's `fingerprint` exits 4 (`native_files.py:114-115`). `read(limit=64 MiB)` for sections, `write_if` (sections with `preserve_metadata=True`), `remove_if`, `prune_empty_parents`, all with `elevated=root`. `_mode` and the new `_directory_mode`. `ROOT_FILE_DIRECTORIES` and the root path rule move here; `native_path` becomes `files.guest_path`                                                                                                                                                      | no deadline today or now                                            |
| Setup dispatch, `harness_setup/dispatch.py:41-56`, `:106-111`, `:208-217`                                                                | `destination_id` runs `cat /etc/machine-id` and `stat -Lc` with `timeout=15` (`:47`, `:52`); publication gets `call.runner`; `ROOT_FILE_DIRECTORIES` from `native_files` (`:30`)                                                                                                                                                   | `destination_id(vm, access, ...)` runs `Command(("cat", "/etc/machine-id"), deadline=15)` and `Command(("stat", "-Lc", "%d:%i:%u", "--", location), deadline=15)`, as the HLA keeps them (hla.md, release behaviors). Publication gets `call.access`                                                                                                                                                                                                                                                                                                                                                                                                               | `ExecutionIncomplete`; setup fails, as a timed-out probe does today |
| Setup readiness, `harness_setup/readiness.py:68-70`                                                                                      | `destination_id(vm, runner)`; `except (SSHError, StateError)` reads as unavailable                                                                                                                                                                                                                                                 | `require_setup_ready` and `_applicable_evidence` take a `VMAccess`; `except (ConnectivityError, StateError)`, which covers `ExecutionIncomplete` and every execution error of the state kind. The session callers (`sessions/manager/_create.py:219`, `_create_roll.py:235`, `_lifecycle.py:758`) pass the session identity's access                                                                                                                                                                                                                                                                                                                               | reads as unavailable                                                |
| Setup lifecycle, `harness_setup/lifecycle.py:162-281`                                                                                    | Wraps the target in `SetupRunner` (`:179`, `:228`, `:269`)                                                                                                                                                                                                                                                                         | Builds the invocation's `access` for the same identity; `SetupRunner` goes with the last `runner` reader                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | none                                                                |
| Session artifacts, `artifacts/session.py:36-236`                                                                                         | `runner.run('printf "%s" "$HOME"')` (`:76`, `:220`); publication through the runner (`:152`, `:190`, `:228`)                                                                                                                                                                                                                       | Each function takes a `VMAccess`. `HOME` is `Script('printf "%s" "$HOME"', Shell.SH)`, which sees the passwd home agw-launch sets (D4). The callers in `sessions/manager/` and `workspaces/manager/delete.py:190` build it                                                                                                                                                                                                                                                                                                                                                                                                                                         | no deadline today or now                                            |
| Native probing, `artifacts/native/probe.py:370-437`                                                                                      | `require_python3` (`:384`), then `"$SHELL" -lic 'python3 -c <probe>'` with the request on stdin, `timeout=30` (`:402-410`)                                                                                                                                                                                                         | `PythonProgram(_PROBE, login=True, interactive=True, env=..., stdin=Bytes(request), deadline=30)`. `require_python3` goes: every request proves python3 or is `VMNotReady` (D4). The `AGW_ARTIFACT_PROBE=` parse and the diagnostics redaction (`:411-437`) stay, on `result.stdout` and `result.stderr` decoded. Runs `/usr/bin/python3`, not the login `PATH`'s                                                                                                                                                                                                                                                                                                  | `ExecutionIncomplete`; setup or the session start fails, as today   |
| Native settings merge, `plugins/_harness_native/native.py:97-397`                                                                        | `NativeFiles(invocation.runner)`: `read`, `publish(expected=...)` that fails on a concurrent change (`:123`, `native_files.py:389-390`), `directory(root)` and `directory(root, create=True)` (`:238`, `:335`)                                                                                                                     | `invocation.access.files`: `read` for the plan and the inventory checks, `update` for each publish with the plan's computation as `transform` (the initial skip decision kept, `:109-110`), `stat` for the root (`Directory` exists, `Absent` does not, a file is `PathRefused` as today), `ensure_directory(root, mode=0o700)`. User files 0600 with parents 0700; workspace files 0660 with parents 2770 and the group. A concurrent change is now retried, up to 3 times                                                                                                                                                                                        | none                                                                |
| Native CLI, `plugins/_harness_native/native_cli.py:97-324`                                                                               | `NativeFiles`' staging directory is the working directory and holds outputs (`:122-144`, `:148-185`, `:308-315`); resolution runs `$SHELL -lc` inside `python3 -c` with the request on stdin, `timeout=30` (`:29-57`, `:123-131`); commands run as a `bash` script on stdin with stderr to `/dev/null`, `timeout=180` (`:170-176`) | A guest scratch directory owned by `NativeCLI` (below). Resolution is `PythonProgram(_LOGIN_RESOLVE_PROGRAM, login=True, stdin=Bytes(request), env=..., deadline=30, log_streams=False)`: the request, with any explicit `PATH`, stays on stdin and out of the logged args, and the program prints one marked JSON line. Each command is `Script(shlex.join([executable, *args]), Shell.BASH, cwd=scratch, env=..., deadline=180, log_streams=False)`, so its line stays out of the log as today; stdout `CaptureOutput(16 MiB)` when structured, else `DiscardOutput`; stderr `DiscardOutput`. `test -e` and `mkdir -p -m 700` become `Command`s (`:274`, `:310`) | `ExecutionIncomplete`; setup fails, as today's `ExternalError` did  |
| Codex guidance, `plugins/codex/artifacts.py:61-66`                                                                                       | `NativeFiles(runner).read(override)`                                                                                                                                                                                                                                                                                               | `access.files.read(override)`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | none                                                                |
| Claude prompt snapshot, `plugins/claude/artifacts.py:178-201`                                                                            | `"$SHELL" -lic 'claude --version'`, `timeout=20`; `except SSHError` warns                                                                                                                                                                                                                                                          | `Script("claude --version", Shell.USER_DEFAULT, login=True, interactive=True, env=..., deadline=20)`; the version regex runs on the decoded stdout; `except ConnectivityError` warns as today                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | warns and launches without `--system-prompt-snapshot`               |
| Harness setup facets: claude `:209-280`, codex `:465-548`, grok `:164-215` (`harness_integration.py`)                                    | `probe_native(invocation.runner, ...)`, `guidance_path(invocation.runner, ...)`                                                                                                                                                                                                                                                    | `invocation.access`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | as the probe                                                        |
| Harness start stages: claude `:324-420`, `:495-537`; codex `:558-625`, `:707-830`, `:900-930`, `:1148-1340`; grok `:224-290`, `:363-396` | `ctx.admin_target()` or `agent_target()`; transcript, rollout, recorder and session probes as `"$SHELL" -lic '<inner>'`, reading exit codes; codex parses uuid lines (`:1191-1193`) and rollout paths from stdout                                                                                                                  | `ctx.admin_access()` or `agent_access()`; each probe is `Script(inner, Shell.USER_DEFAULT, login=True, interactive=True)` with its exit-code protocol unchanged, built by one named function per probe. Codex's line parsers run on the decoded stdout, still skipping login-shell noise. `require_completed` raises the typed transport error where today's 255 became the probe's own `StateError`                                                                                                                                                                                                                                                               | no deadline today or now                                            |
| Harness readiness, `capabilities/harness_integration/base.py:130-191`, `:462`, `:483-532`; `shell.py:154`; each `_probe_target`          | Reads the targets; `require_commands(commands, transport)`                                                                                                                                                                                                                                                                         | Reads the accesses; `_probe_target(access)` and `require_commands(commands, access)`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | no deadline today or now                                            |
| Command checks, `command_checks.py:15-77`                                                                                                | `"$SHELL" -lic` with a status line on descriptor 3 and `discard_output`; `SSHError` when indeterminate                                                                                                                                                                                                                             | `run_user_shell_command(command, access, *, deadline=None)` runs `Script(probe, Shell.USER_DEFAULT, login=True, interactive=True)` where `probe` is `( command ) >/dev/null 2>&1`, then `printf 'agentworks-command-status:%s\n' "$?"`. Only the last stdout line matching that form is read, so the command's own output is suppressed as `capabilities/git_credential/README.md:96-99` promises and startup noise cannot forge a status; stderr is `DiscardOutput`. A missing line or a non-`Completed` outcome is indeterminate: `ExternalError`, or the typed transport error                                                                                  | the callers' (below)                                                |
| Git-credential providers, `capabilities/git_credential/github.py:156-186`, `plugins/azure/azdo.py:138-166`                               | `ctx.admin_target() or ctx.agent_target()`, `timeout=10`; `except SSHError` warns                                                                                                                                                                                                                                                  | `ctx.admin_access() or ctx.agent_access()`, `deadline=10`; `except (ConnectivityError, ExternalError)` warns as today. A `StateError` such as `VMNotReady` now propagates: runup runs right before the operations that need that VM. `_GH_AUTH_CHECK` and `_AZ_AUTH_CHECK` unchanged                                                                                                                                                                                                                                                                                                                                                                               | "Could not determine ... readiness", as today                       |
| `RunContext` construction sites (above)                                                                                                  | Pass targets                                                                                                                                                                                                                                                                                                                       | Pass accesses instead                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | none                                                                |

**The native CLI's scratch directory stays.** It is made with
`Command(("mktemp", "-d", "-t", "agentworks-native-XXXXXXXXXX"))` and removed with
`Command(("rm", "-rf", "--", path))` when `NativeCLI` closes, as `NativeFiles.__enter__` and
`__exit__` do (`native_files.py:265-291`); it is each command's working directory and the parent of
the isolated discovery homes (`native_cli.py:302-320`). I could not establish that the harness CLIs
leave their working directory alone. A quick run on the baseline host of
`claude plugin list --json`, `claude plugin marketplace list --json` and the codex equivalents in an
empty directory, under an isolated home with the network blocked as the fixtures do, left it empty
but every command exited 1, so it measured nothing; the commands that matter, `install` and
`marketplace add`, need a live source. Claude also decides project scope from its working directory,
which is why setup passes `--scope user` (`native_cli.py:277-298`), and the user's home as working
directory would make `~/.claude` both the user and the project directory.

**Callers outside the plan's list.** `grok`, the `shell` integration, `harness_setup/dispatch.py`,
`readiness.py` and `lifecycle.py`, `git_credentials/__init__.py`, and every `RunContext`
construction site move here because signatures change under them. None needs anything new.

**Test fakes.** The session and harness unit tests that use `_FakeTarget`, a substring-to-result map
(`cli/tests/conftest.py:452`), move to `RecordingVMAccess` with outcomes keyed by probe: each
probe's request comes from one named builder, and the fake matches a request equal to that
builder's, so a test names the probe rather than a fragment of its text.

### Deleting the legacy modules

`native_files.py` and `harness_setup/runner.py` go once their last reader moves and their rows (I35
to I43, I47, I50 and I51) are re-proved (R10). With them:

- `cli/tests/execution/legacy_modules.py:9-10` drops both names, so the blocker
  (`cli/tests/execution/conftest.py:21-45`) and the static check stop naming modules that no longer
  exist;
- the static check's form cases that use them (`cli/tests/test_execution_import_boundary.py:98-99`)
  switch to `agentworks.remote_exec` and `agentworks.transports.base`;
- `cli/agentworks/execution/README.md:197-198` lists the four remaining modules;
- `cli/tests/test_native_file_boundaries.py`, `test_native_path_failures.py` and
  `tests/artifacts/test_generated_sections.py:226-247` move to `file_program.py`, patching
  `files.program_source` where they patched `_FILE_PROGRAM`;
- HLA migration item 8 and plan slice 8 stop listing them (Amendments).

**The package guard.** The plugins stay on the static check because importing one loads the plugin
index (`cli/tests/test_execution_import_boundary.py:44-45`). Batch C checks whether the index then
imports no legacy module; if so, the plugins move to the fresh-interpreter guard (D14), and if not,
the module that still does is recorded there.

## Tests

None restates the implementation or our prose (`no-prose-policing-tests`).

**The plan's Tests box:**

| Plan item                                                                   | Test                                                                                                                                                                                                         |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `write_if` and `remove_if` detect a concurrent writer                       | The test changes the destination after the caller read it; the operation raises `FileConflict` and the destination keeps the test's bytes. Two concurrent `write_if`s with one expectation: exactly one wins |
| a truncated stdin publishes nothing                                         | A `write_if` whose stdin ends short of `size`, and one whose bytes differ from `sha256`: status 6, the destination untouched, no temporary file, no created parent                                           |
| JSON null survives an update                                                | `update` with the settings merge over an existing document holding `null`                                                                                                                                    |
| a malformed destination is refused                                          | `update` with a merge strategy over malformed JSON and TOML raises `ConfigError`; the file is byte-identical                                                                                                 |
| a symlinked destination is refused                                          | A link at the destination and at a parent, for every operation: `PathRefused(UNSUITABLE)`, and the link's target is untouched                                                                                |
| cleanup touches only what the operation created                             | A conflict, a denied rename and an attribute-copy failure each leave no `.agentworks-tmp-*`; an unrelated pre-existing one survives; created parents remain                                                  |
| `preserve_metadata=True` keeps owner, group, mode and ACLs                  | `cli/tests/artifacts/test_generated_sections.py:193-265`, moved; owner and group under the sudo lane (below)                                                                                                 |
| a request's stdin is absent from logs and exceptions (a conformance vector) | Vector F1                                                                                                                                                                                                    |

The plan's two other items, "`write` applies mode and owner before the rename" and "a tree upload
replacing the directory above the target", go to slice 6 with `write` and `upload_tree`.

**Against a real local filesystem.** `cli/tests/execution/test_file_program.py` runs the program's
`main` in subprocesses (`python3 -I -S -c` importing the module and calling
`main(argv, lock_path=..., lock_wait=...)`), so operations and the test itself can contend, on a
temporary tree with `lock_path` in a sticky, world-writable temporary directory, on Linux CI
(skipped elsewhere: `O_PATH` is Linux's):

- the lock file is created 0644 under umask 077, and one another identity created first is opened,
  not recreated (sudo lane, below; the runner's own `protected_regular` applies, and F6 proves
  Debian's 2);
- a symlink planted at the lock path is `PathRefused(UNSUITABLE)` and is not followed;
- a held lock past `lock_wait` (0.2 s in the test) is `FileLockTimeout`, with nothing created;
- `read` and `stat` succeed while another process holds the lock;
- a search-only (0711) ancestor works (I36);
- `stat` reports a directory without opening it, and a FIFO destination is refused without blocking;
- `prune_empty_parents` keeps a verified-empty root whose parent denies removal, and errors when the
  root was replaced after the walk (I41);
- a `SIGTERM` during a held `write_if` exits nonzero and leaves no temporary file.

The sudo lane runs where `sudo -n true` succeeds. CI's Linux jobs already use passwordless sudo
(`.github/workflows/ci.yml:83-84`), so it is required there and skipped locally, as the S lane
treats `sshd`.

**Unit, every CI host:** the envelope round trip for `python` and `interactive`; the status-to-error
mapping for 0 to 8 and an unknown status; `update` retries a conflict, stops after three, and does
not retry `FileLockTimeout` or `Unknown`; `transform` returning `None` or the same bytes writes
nothing; a `format_record` of a request with `Bytes(marker)` and of a result whose streams hold
`marker`, under `log_streams=False`, does not contain the marker's bytes; `stream` refuses `Bytes`
with `ValidationError`; `RunContext` construction is passive; `guest_path`'s refusals; `files` on an
agent access refuses `elevated=True` before dispatch; `SetupInvocation.request_environment` keeps
core's `AGENTWORKS_*` values and drops a plugin's
(`cli/tests/capabilities/test_native_setup_runner.py`, moved).

**Caller tests stay green.** The native setup, publication and probe tests already run the real
guest program against local files on Linux (`cli/tests/native_setup_fixtures.py:20-80`,
`cli/tests/test_native_harness_setup.py:34`). They move from `LocalFixtureTransport` to a local
`VMAccess` built on the L lane (`cli/tests/execution/conformance/lanes.py`), promoted to
`cli/tests/vm_access_support.py`.

### Conformance vectors

Lanes as `execution-lld.md` defines them; V runs on a Debian 12 and a Debian 13 guest, and F4 and F6
on every platform's V lane.

| Vector                                                                                                                                                                                                                                                                                                                                                                 | Lanes |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| F1 A `Bytes(secret)` stdin to a payload that does not echo it, with `log_streams` true and false: the secret is in no `SSHLogger` file, error message, hint or `repr`. A payload that echoes it to both streams and exits 3 under `check=True`, with `log_streams=False`: the same, and the record says the streams were not logged; with `True` the record holds them | L S V |
| F2 `Bytes` with every byte value, NUL included, reaches a `PythonProgram`'s `sys.stdin.buffer` exactly, at its first byte                                                                                                                                                                                                                                              | L S V |
| F3 `PythonProgram` arguments: empty, whitespace and shell characters arrive exactly; the source is absent from `/proc/<pid>/cmdline`                                                                                                                                                                                                                                   | L S V |
| F4 `PythonProgram` with `login`, `interactive` and both, under `sh`, `dash`, `bash` and `zsh`, with startup files that export, print and end in a failing command: the export is visible, the arguments are intact, the program runs                                                                                                                                   | L V   |
| F5 `Script.interactive` under each shell: an interactive startup file's function is callable, and the sourced script's `exit N` is the status                                                                                                                                                                                                                          | L V   |
| F6 The lock under the image's `protected_regular`: created by the agent, then taken by the admin unelevated and elevated, and the reverse; a `write_if` by each in turn completes                                                                                                                                                                                      | V     |
| F7 Elevated `write_if` and `read` of a root-owned file, and an agent's elevated `write_if` refused before dispatch                                                                                                                                                                                                                                                     | S V   |
| F8 An elevated `Command(("env",), env={"AGW_F8": "a b"})` prints `AGW_F8=a b`                                                                                                                                                                                                                                                                                          | L V   |

F6 is the HLA's risk-table proof for the lock (hla.md, risks).

## Incident audit

The entry points this slice moves are `native_files.py`, `artifacts/publication.py`,
`artifacts/session.py`, `artifacts/native/probe.py`, `plugins/_harness_native/`,
`harness_setup/runner.py`, the harness integrations' probes, `command_checks.py` and the two
CLI-readiness providers. The audit read their fix history (`git log` on each file), the native
harness setup and harness facets guides, and the runtime git identities SDD's plan
(`docs/sdd/2026-08-28-runtime-git-identities/plan.md`). It proposes these rows for the HLA's
inventory, whose last row is I34 (`hla.md:850`):

| Row | Behavior                                                                                                                                                                                                                                                                                                                                                               | Re-proved in |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| I35 | A native file is never read or written through a symbolic link, at the destination or any parent, and the refusal names the path and tells a link or wrong type from a denial (`25e972553`, `a2eaba12e`; `native_files.py:20-40`, `:109-115`, `:199-215`)                                                                                                              | 5            |
| I36 | Traversal needs only search permission on each ancestor, so a search-only directory above a destination does not block setup (`93a4e1a6c`; `native_files.py:32`; `docs/guides/native-harness-setup.md:49`)                                                                                                                                                             | 5            |
| I37 | A conditional publication checks the destination's digest and replaces it atomically from a sibling temporary file, so a concurrent edit fails it instead of being overwritten (`25e972553`; `native_files.py:140-184`; `cli/tests/artifacts/test_generated_sections.py:109`)                                                                                          | 5            |
| I38 | Generated-section updates and retirements keep the existing file's owner, group, mode, extended attributes and ACLs, drop an ACL the new file inherited from its directory, and leave the original untouched when the copy fails (`0decc2a75`, `3b3b9f69c`; `native_files.py:155-172`; `cli/tests/artifacts/test_generated_sections.py:193-265`)                       | 5            |
| I39 | Owned-file verification compares digest and mode on the guest without copying the content back (`4f4ab01ee`; `native_files.py:425-446`; `artifacts/publication.py:107`, `:137`)                                                                                                                                                                                        | 5            |
| I40 | Elevated native file access stays inside the dedicated root directories for desired and recorded paths alike, and retirement keeps the boundary directories (`66f0edb54`; `native_files.py:193`, `:229-238`; `artifacts/publication.py:89-100`)                                                                                                                        | 5            |
| I41 | Retirement prunes only empty parents up to the recorded package root, and keeps, with a warning, an empty root its parent will not release, after checking it is the directory it walked (`55ff7aee3`, `ad5a9733a`, `cd735e8b6`; `native_files.py:41-81`, `:408-423`)                                                                                                  | 5            |
| I42 | Native file contents and native CLI commands and output stay out of the operation log (`25e972553`; `native_files.py:334-354`; `plugins/_harness_native/native_cli.py:170-185`; `docs/guides/native-harness-setup.md:55-57`)                                                                                                                                           | 5            |
| I43 | Native executables are resolved once from the actual user's login `PATH`, and an explicit `PATH` in the setup environment wins (`7f4a79b5c`; `plugins/_harness_native/native_cli.py:29-57`, `:118-144`)                                                                                                                                                                | 5            |
| I44 | Session-resume probes (Claude's transcript, Codex's recorder and rollouts, Grok's session) read their exit code, so a probe that could not run never reads as no conversation and never launches over one (`86de4c6a3`, `a3965d7d8`, `fb801c5e3`; `plugins/claude/harness_integration.py:495-537`; `plugins/codex/harness_integration.py:1148-1340`)                   | 5            |
| I45 | Probe diagnostics have the setup environment's values removed, raw and shell-quoted, before they reach an error (`15b714480`, `5b51afcab`; `artifacts/native/probe.py:411-417`)                                                                                                                                                                                        | 5            |
| I46 | Native discovery requests travel on stdin, so a large package needs no argument (`14a4b1336`; `artifacts/native/probe.py:402-410`)                                                                                                                                                                                                                                     | 5            |
| I47 | A settings write that finds nothing to change leaves the file and its permissions alone (`9e20740e7`, `e77d1a24f`; `plugins/_harness_native/native.py:122-123`; `docs/guides/native-harness-setup.md:52-53`)                                                                                                                                                           | 5            |
| I48 | GitHub and Azure CLI readiness is checked read-only in the target user's shell with the CLI's output suppressed, never blocks helper installation, and warns when the result is indeterminate (`f72884fdd`, `11e1e62ba`, `b2f1dc992`; `capabilities/git_credential/github.py:156-186`; `plugins/azure/azdo.py:138-166`; `capabilities/git_credential/README.md:96-99`) | 5            |
| I49 | A required-command check runs in the user's login and interactive shell and reports the command's own status apart from a startup failure (`b2f1dc992`; `command_checks.py:15-77`)                                                                                                                                                                                     | 5            |
| I50 | A setup request's prepared environment reaches it unchanged when it runs elevated (`5797e5110`; `harness_setup/runner.py:54-57`)                                                                                                                                                                                                                                       | 5            |
| I51 | A plugin's request environment cannot override or add `AGENTWORKS_*` identity values (`bcca20a86`; `harness_setup/runner.py:29-37`; `docs/guides/harness-facets.md:189-192`)                                                                                                                                                                                           | 5            |
| I52 | When Claude's version cannot be read in the login environment, the session-prompt workaround warns and launches without `--system-prompt-snapshot` instead of failing (`21a9b4b7d`; `plugins/claude/artifacts.py:178-201`)                                                                                                                                             | 5            |
| I53 | The native probe finds the harness command and its native home override (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GROK_HOME`) in the user's login and interactive environment, so one set only in a dotfile is honored and a missing command is reported before publication (`3b4ea08b6`; `artifacts/native/probe.py:28-49`)                                                | 5            |

**How each is re-proved.** I35 to I41 by the program and publication tests, I38's owner and group in
the sudo lane and its ACLs live; I42 by F1 and a native setup test that reads its log; I43 by the
native CLI discovery tests (`cli/tests/test_native_cli_discovery.py`) on the local access; I44 by
the probe protocol tests over the recording fake and F5; I45, I46 and I53 by the probe tests and F4;
I47 by the `update` unit test and the native setup tests; I48 and I49 by the provider readiness and
command check tests, with a startup file that exits 0 and a command that prints; I50 by F8; I51 by
`request_environment`'s test; I52 by the prompt snapshot test. Each row also gets a live cell.

`2841cb9fa` (python3 guaranteed for native helpers) needs no row: D10 makes python3 a provisioning
requirement, and every request is `VMNotReady` without it.

## Collateral

- **ADR 0019** (the plan's box): `:86` names `ctx.admin_target()` and `ctx.agent_target()`; the
  status block gains an "Amended" paragraph naming `admin_access()` and `agent_access()`.
- **Plugin author documentation:**
  - `capabilities/README.md:220-229` and `capabilities/vm_platform/README.md:356-358`, which both
    describe the context's accessors as returning execution `Transport`s;
  - `capabilities/harness_integration/README.md:458`, `:578`, `:671-678` (probes as `Script` with
    `login` and `interactive`, `check_required_commands` on an access) and `:684-705` (the test
    fakes);
  - `capabilities/git_credential/README.md:96-99` stays true;
  - `artifacts/README.md:426` (publication over `access.files`);
  - the harness-integration and git-credential contract version mentions.
- **`docs/guides/harness-facets.md:189-192`:** "environment arrives separately through the prepared
  runner" becomes the invocation's request environment.
- **`docs/guides/native-harness-setup.md`:** `:8-13` (python3 is proven by every request and
  `VMNotReady` names recover; there is no staging), `:46-53` (publication under the VM-wide lock; a
  concurrent change is retried before it fails), `:55-57` (unchanged promise).
- **`cli/agentworks/execution/README.md`:** `Bytes`, `log_streams`, the sinks, `PythonProgram`,
  `Script.interactive`, `files` and the lock, and `:197-198`'s module list;
  `cli/tests/execution/README.md`: the F vectors and the sudo lane.
- **`docs/guides/upgrading-to-0.20.md` and release notes:** the probe runs `/usr/bin/python3`;
  settings conflicts are retried; a `VMNotReady` VM now fails a gh or az runup; the contract
  versions.
- **The HLA's inventory** gains rows I35 to I53.
- **Completions and sample config:** no command, option or setting changes.

## Live evidence

The plan's cells: harness setup and a session start with artifacts, on Remote Lima from macOS and on
one cloud from Windows. Each runs the V vectors and:

- user and workspace setup with a settings mapping, then a second setup that changes nothing (I47);
- VM-scope artifacts elevated into `/etc/claude-code` (I40), and a generated section over a file
  with an ACL (I38);
- a session start that resumes from a transcript (I44), with an agent's access;
- `gh` readiness with and without a login (I48);
- F4 and F6 on every platform's V lane; agw creates Debian 12 only on Proxmox (plan, slice 2), so
  the Debian 12 cell may join the carried-forward Debian 12 evidence.

## Delivery

Three batches, each green on its own, in the order they land in PR C:

- **A, probes and delivery.** The request vocabulary (`Bytes`, `log_streams`, the sinks,
  `PythonProgram`, `Script.interactive`) with vectors F1 to F5 and F8; `RunContext.admin_access()`
  and `agent_access()` with every construction site and the slice 6 signature changes, deleting
  `admin_target()` and `agent_target()`; `SetupInvocation.access` with `runner` kept beside it;
  `command_checks.py`; the harness readiness and start probes and `probe.py`, since the start stages
  call `probe_native`; the claude prompt snapshot; the two CLI-readiness providers; the contract
  bumps.
- **B, files and publication.** `file_program.py`, `files.py`, the errors, the local filesystem and
  sudo-lane tests, F6 and F7; `dispatch.py`, `readiness.py` (with `SetupReadiness.runner` deleted),
  `artifacts/publication.py`, `artifacts/session.py` and codex's `guidance_path`, with their
  callers.
- **C, native setup and deletions.** `native.py` and `native_cli.py`; `request_environment`;
  deleting `SetupInvocation.runner`, `SetupRunner`, `native_files.py` and `require_python3` with the
  guard updates above; the package guard check; the inventory rows.

Each batch carries the collateral it makes true.

## Open questions

None. The lead answered the first draft's eight on 2026-11-05: `log_streams` on the request; `stat`
as a port of `fingerprint` and `directory`; `SetupRunner` deleted with its rule kept on
`SetupInvocation`; the targets deleted and each contract bumped once; `command_checks.py` and the
deletion of `native_files.py` in this slice; one JSON argument with `size` and `sha256`; a 30 s
wait.

**One departure from the lead's wording.** The lead's B2 fix named
`exec 9<"$0" && exec /usr/bin/python3 -I -S /dev/fd/9 "$@"`. Measured on the baseline host, `&&`
fails under `zsh -i` and `zsh -l -i` whenever the startup's last command fails, because zsh leaves
`$?` at that status after an `exec` with only a redirection; `;` works in all sixteen cases
(`PythonProgram`, above). This LLD uses `;`.

## Amendments

The HLA and the plan need these changes; this LLD does not edit them. Quoted text is current.

**HLA D5.**

- Table row "Common options": `` `env`, `cwd`, `elevated`, `deadline` (not `LoginShell`) `` becomes
  `` `env`, `cwd`, `elevated`, `deadline`, `log_streams` (not `LoginShell`) ``.
- Table row "Input": `` `NoInput`, `Bytes(data, sensitive=)`, `FromFile(path)` `` becomes
  `` `NoInput`, `Bytes(data)`, `FromFile(path)` ``.
- Table row "Output sink (buffered)":
  `` `Capture(limit)`, `Discard`, `ToFile(path, on_progress=)` `` becomes
  `` `CaptureOutput(limit)`, `DiscardOutput`, `ToFile(path, on_progress=)` ``, and its consumers
  `` `Discard`: `command_checks.py`, native CLI `` become
  `` `DiscardOutput`: `command_checks.py`, native CLI ``.
- "That is how the native probe and the native CLI's login resolution keep seeing the native
  workload's login and interactive environment, as they do today through `"$SHELL" -lic`
  (`probe.py:402-410`)." becomes "That is how the native probe keeps seeing the native workload's
  login and interactive environment, as it does today through `"$SHELL" -lic` (`probe.py:402-410`),
  and the native CLI's login resolution its login environment (`native_cli.py:44`)."
- Bullet "Sensitive inputs": "Retained output has the request's secret values redacted (D13), and an
  explicitly sensitive stdin marks the streams as not retained at all." becomes "Retained output has
  the request's secret values redacted (D13), and a request with `log_streams=False` keeps both
  streams out of the log. Stdin is never formatted into a record or an error."

**HLA D6.**

- Table row `write`: "A temporary file in the destination directory, filled from stdin with owner
  and mode set and `fsync`ed without the lock, then renamed into place under it." becomes "Content
  read whole from stdin and checked against its size and digest, then, under the lock, a temporary
  file in the destination directory with owner and mode set, `fsync`ed and renamed into place." Its
  consumers "(5, 6)" become "(6)".
- A new row after `read`: `` `stat(path) -> Absent | Directory | RegularFile(digest, mode)` `` |
  "`lstat` relative to the parent; only a regular file is opened, without following links, and
  hashed on the guest" | "artifact publication, native settings (5)".
- "the file program opens it read-only without following symlinks" becomes "the file program opens
  it read-only, without following symlinks or blocking".
- "A tree upload extracts into a temporary directory beside its destination without the lock and
  takes it only to move the result into place (the old tree aside and the new one in), and a `write`
  likewise stages its temporary file first, so every hold is short, and a lock wait has a deadline."
  becomes "A tree upload extracts into a temporary directory beside its destination without the lock
  and takes it only to move the result into place (the old tree aside and the new one in), and a
  `write` reads its content whole before it takes the lock and then holds it from its walk to its
  rename, so every hold is short, and a lock wait has a deadline."

**HLA D13.**

- "A sensitive stdin keeps the streams out of the log entirely; that rule lands with
  `Bytes(data, sensitive=)` in slice 5, the first request to carry one." becomes "A request with
  `log_streams=False` keeps both streams out of the log entirely; the option lands in slice 5 with
  its first callers, the file program's reads and the native CLI."
- The error table gains `FileConflict`, `PathRefused` and `FileLockTimeout`, each `StateError`.

**HLA migration.**

- Item 5: "`files.py`, the `RunContext` accessors, and finite stdin with `Bytes(data, sensitive=)`
  and its conformance vector. Consumers: harness, artifact, native-file and git-credential." becomes
  "`files.py`, the `RunContext` accessors, and finite stdin with `Bytes(data)` and `log_streams`,
  with their conformance vectors. Consumers: harness, artifact, native-file, command-check and
  git-credential. `native_files`, `harness_setup.runner`, `admin_target()` and `agent_target()` are
  deleted."
- Item 6: "VM and agent initializers, hardening, DNS, sources and the setup runners." becomes "VM
  and agent initializers, hardening, DNS and sources."
- Item 8: "Delete the remaining legacy modules (slice 4 deletes the Proxmox transport),
  `admin_target()` and `agent_target()`, doctor's" becomes "Delete the remaining legacy modules
  (slice 4 deletes the Proxmox transport, and slice 5 `native_files` and `harness_setup.runner`),
  doctor's".
- The incident inventory gains rows I35 to I53 from this LLD.

**Plan slice 5.**

- The first box: "Write [`files-lld.md`](./files-lld.md). It covers:" is ticked once this revision
  passes review.
- "Implement `PythonProgram`, `files.py` and the file program, finite stdin for `run` with
  `Bytes(data, sensitive=)`, and `RunContext.admin_access()` and `agent_access()`." becomes
  "Implement `PythonProgram`, `Script.interactive`, finite stdin for `run` with `Bytes(data)`, the
  `log_streams` option, the `CaptureOutput` and `DiscardOutput` sinks, `files.py` and the file
  program, and `RunContext.admin_access()` and `agent_access()`."
- "Make the operation log keep the streams of a request with a sensitive stdin out entirely (HLA
  D13), now that `Bytes(data, sensitive=)` gives it a first caller." becomes "Make the operation log
  keep both streams of a request with `log_streams=False` out entirely (HLA D13), with its first
  callers, the file program's reads and the native CLI."
- The Migrate list gains "`command_checks.py`;" and "the harness setup dispatch, readiness and
  lifecycle, with `SetupInvocation.access`;".
- A new box after Migrate: "Delete `admin_target()`, `agent_target()`, `SetupInvocation.runner`,
  `SetupRunner` and `native_files.py` with their last readers, after re-proving their incident rows,
  and bump the harness-integration contract to 8 and git-credential to 4."
- Tests: "`write` applies mode and owner before the rename;" moves to slice 6. "`write_if` and
  `remove_if` detect a concurrent writer, including a tree upload replacing the directory above the
  target;" becomes "`write_if` and `remove_if` detect a concurrent writer;", with "a tree upload
  replacing the directory above a target is detected" moving to slice 6. "a truncated stdin
  publishes nothing;" is added. "a sensitive stdin is absent from logs and exceptions (a conformance
  vector)." becomes "a request's stdin is absent from logs and exceptions, and `log_streams=False`
  keeps its streams out (a conformance vector)."
- Definition of done: "`native_files.py` has no remaining callers." becomes "`native_files.py` and
  `harness_setup/runner.py` are deleted, and no capability reads a legacy transport."

**Plan slice 6.**

- Migrate: "`sources.py` (with `upload_tree`), `command_checks.py` and `git_config.py`;" becomes
  "`sources.py` (with `upload_tree`) and `git_config.py`;", and "the harness setup runners. The
  `SetupRunner` environment rules become request environment." is removed (slice 5 does it).
- A new Tests box: "`write` applies mode and owner before the rename, and a tree upload replacing
  the directory above a `write_if` target is detected."

**Plan slice 8.**

- "Delete the remaining legacy modules with their tests (slice 4 deletes the sixth,
  `agentworks.plugins.proxmox.transport`):" becomes "Delete the remaining legacy modules with their
  tests (slice 4 deletes `agentworks.plugins.proxmox.transport`, and slice 5
  `agentworks.harness_setup.runner` and `agentworks.native_files`):", and its list loses those two
  bullets.
- "Delete `RunContext.admin_target()` and `agent_target()`, and the `SSHError` special case in
  `cli/_entry.py`." becomes "Delete the `SSHError` special case in `cli/_entry.py`."
