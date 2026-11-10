# Transport Take 2: Initialization Low-Level Design (Slice 6)

- Status: Revised after the private design reviews; the lead's rulings and the operator's ruling of
  2026-11-09 applied.
- Date: 2026-11-09
- Architecture: [hla.md](./hla.md) D5 (requests), D6 (files), D12 (delivery to consumers), D13
  (errors and the operation log), D14 (the guards); requirements: R4, R5, R9, R10, R11
- Plan: [plan.md](./plan.md), slice 6, delivered in PR C
- Source baseline: `747f486a0` (the PR C branch head). Every `path:line` is at that commit. Paths
  are under `cli/agentworks/` unless they start with `cli/` or `docs/`.

## Scope

This LLD settles slice 6: Phase B and the agent initializer on `VMAccess`, the file operations they
need (`write` and `update`, and `ensure_directory`'s owner), the dotfiles tree upload, the
replacement of every hand-written write in the code they reach, the git-credential and grant paths,
and the incident audit of what moves.

**Operator direction applied.** Every operation and parameter below has a caller in this slice.
Unsupported setups are refused with an alert rather than handled by new machinery (HLA, operator
decision 10), and related problems found on the way are fixed here (FRD, 2026-11-08: the operator
leans towards fixing anything related now rather than filing it).

**What exists at HEAD.** Phase A runs on `VMAccess` (`vms/initializer/phase_a.py`). Phase B receives
the legacy `SSHTransport` that `bootstrap_vm` builds with `default_timeout=60`
(`vms/initializer/driver.py:139-147`), or that reinit builds the same way
(`vms/manager/reinit.py:269`). Its only execution-core uses are the git-credential providers' runup
(`driver.py:553-560`) and harness setup (`harness_setup/lifecycle.py:180-181`). The agent
initializer runs on the legacy `transport()` and `transport_for_user()` with no default bound
(`agents/initializer.py:121`, `:224`; `transports/__init__.py:42-87`). The file program has `read`,
`stat`, `write_if`, `remove_if`, `ensure_directory` and `prune_empty_parents`
(`execution/file_program.py:409-415`).

**Callers outside the plan's first list.** Phase B and agent create also call `apply_workspace_acls`
(`workspaces/acls.py:20-51`, from `vms/initializer/workspaces_dir.py:63`) and the tmux socket
helpers (`sessions/tmux.py:240-413`, from `driver.py:493-501` and `agents/initializer.py:205-207`).
Create and reinit cannot run entirely on the new transport (the plan's definition of done) unless
these move here, so they do, with their slice 7 callers building an access beside their legacy
target, which R10 allows (decision 1).

**Out of this slice.** The unconditional `remove` stays slice 7, with its first caller. The other
hand-written writes in the tree belong to modules later slices migrate: the tmuxinator files
(`workspaces/backends/vm.py:180`, `workspaces/manager/rehome.py:287`,
`workspaces/manager/copy.py:226`, `sessions/manager/_scope.py:165`), tmux's restricted config
(`sessions/tmux.py:519`), the detached wrapper (`remote_exec.py:112`) and backup's path list
(`vms/backup.py:341`). The swap file's `>> /etc/fstab` lines are platform-owned root scripts, not
core writes (`capabilities/vm_platform/bootstrap_script.py:179`,
`capabilities/vm_platform/wsl2.py:808`).

## Module layout

| Module                                                                                       | Change                                                                                                                                                                                       |
| -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `execution/file_program.py`                                                                  | `write`; `ensure_directory` gains `owner`, `group` and elevation; `SEALED` gains `write`                                                                                                     |
| `execution/files.py`                                                                         | `Files.write`, `Files.update`, `ensure_directory(owner=, group=, elevated=)`; the module docstring stops saying every mutation is conditional                                                |
| `execution/errors.py`                                                                        | `CommandFailed.streams_logged`, the failed request's `log_streams`                                                                                                                           |
| `vms/initializer/steps.py` (new)                                                             | `STEP_DEADLINE`, `STEP_FAILURES`, `InitLog`, `describe_failure`, `status`, `warn`, `warn_step`, `update_and_apply`                                                                           |
| `vms/initializer/driver.py`                                                                  | `bootstrap_vm` returns an admin `VMAccess`; `run_initialization` and `_phase_b_setup` take it in place of `ts_target`                                                                        |
| `vms/initializer/shell_env.py`, `ssh_keys.py`, `packages.py`, `mise.py`, `workspaces_dir.py` | Each helper takes `files: Files`; `ensure_source_line` and `source_files` (new, `shell_env.py`) replace the seven grep-or-append strings and their four file lists                           |
| `vms/hardening.py`, `vms/tailscale_dns.py`                                                   | On `Files`                                                                                                                                                                                   |
| `sources.py`                                                                                 | On `Files`; `upload_tree` (one `Script`); `normalize_destination` and `expand_destination`; a local read's `OSError` becomes `SourceRefError`                                                |
| `vms/admin.py`, `agents/template.py`                                                         | `dotfiles_destination` validated by `normalize_destination` at template load                                                                                                                 |
| `git_config.py`                                                                              | On `Files`                                                                                                                                                                                   |
| `git_credentials/__init__.py`, `reconcile.py`                                                | `configure_user_git_credentials(access, ...)` loses `target`; the reconcile is a `Script` on the user's access with the archive as bytes                                                     |
| `agents/initializer.py`, `agents/grants.py`                                                  | On the admin's and the agent's `VMAccess`                                                                                                                                                    |
| `workspaces/acls.py`                                                                         | `apply_workspace_acls(files, path)`; its slice 7 callers build an admin access beside their target                                                                                           |
| `sessions/tmux.py`                                                                           | The socket helpers and stale cleanup take `files: Files`; `access_run_command(access, *, deadline, elevated)` is the tmux probes' `RunCommand` adapter; `create_session` takes `admin_files` |
| `harness_setup/lifecycle.py`                                                                 | `apply_vm_setup` takes Phase B's `files` in place of `config`; `VM_SETUP_DEADLINE` goes                                                                                                      |
| `plugins/_harness_native/native.py`                                                          | The settings publish calls `update(observed=)` and keeps its own conflict hint (decision 2)                                                                                                  |
| `vms/manager/lifecycle.py`, `vms/manager/reinit.py`, `agents/realize.py`                     | Pass the access; reinit reuses the contract's access (`reinit.py:282`) for Phase B                                                                                                           |

## The file program

The program gains one operation, `write`, and `ensure_directory` gains ownership. `update` is a
workstation composition of what the caller read and `write_if`; the dotfiles tree upload is a
`Script` of its own (below), as workspace copy's tar stream is (HLA D6).

| Op                 | Params                                                                 | stdin                  | stdout |
| ------------------ | ---------------------------------------------------------------------- | ---------------------- | ------ |
| `write`            | `path`, `mode`, `owner`, `group`, `parent_mode`, `size`                | `sha256`, then content | none   |
| `ensure_directory` | `path`, `mode`, and now `owner` and `group` (each `null` when not set) | none                   | none   |

`SEALED` (`execution/file_program.py:69`) becomes:

```python
SEALED = {
    "write_if": ("expected", "sha256"),
    "write": ("sha256",),
    "remove_if": ("expected",),
}
```

The digest of content travels on stdin for every operation that takes content, as slice 5 settled
(`files-lld.md:448-451`), so the logged argument holds paths, modes, names and a size, never content
or its digest. No slice 6 write carries a secret (the one secret this slice moves, git credential
material, is a `Script`'s stdin, below), but the rule is the program's, not the caller's.

### `write`

Unconditional replacement with mode and ownership applied before publication (R5).

1. `main` reads stdin to end of file and checks `size` and `sha256` before the operation runs, as it
   does for `write_if`; a mismatch is status 6 with nothing created (`file_program.py:433-442`).
2. Take the lock.
3. Walk the parent, creating missing components with `parent_mode` and `group`'s gid as `write_if`
   does for an absent destination (`file_program.py:100-127`).
4. Unlink a killed write's temporary file, then create `.agentworks-tmp-<24 hex>` 0600 and write the
   content (`file_program.py:265-276`).
5. `fchown(uid, gid)` for the `owner` and `group` given (`-1` for one not given), then
   `fchmod(mode)`: chown first, because it clears set-id bits (`file_program.py:277-280`).
6. Open an existing destination as a regular file without following links: a link or another type is
   status 4, so `write` never replaces a link. This is the type check `write_if` gets from its
   compare (`files-lld.md:228-229`), with no digest to compare.
7. `fsync`, rename over the destination, best-effort `fsync` of the directory; the `finally` unlinks
   the temporary name (`file_program.py:291-306`).

`owner` sets the file's owner only; a created parent is the writing identity's, as for `write_if`.
`write` and `write_if` share one function in the program, differing only in whether step 6 compares
a digest. The existing file's metadata is never kept: every `write` caller states the mode it wants
(HLA D6), and the callers that keep a file's metadata use `update`. An unelevated `write` naming
another account's `owner` fails in the kernel with `EPERM`, which the program reports as status 5,
`PathRefused(DENIED)`; no caller does that, so nothing checks for it first.

Statuses: 0, 1, 4, 5, 6, 7, `128 + n`.

### `ensure_directory` with an owner

Today `ensure_directory` creates a missing directory and leaves an existing one as it is
(`file_program.py:360-366`, `files-lld.md:390-391`), which native settings needs. With `owner` or
`group` (elevated for another account's owner), it also converges the directory, new or existing:
under the lock it walks the parent without following links, creates the directory if missing, opens
it with `O_RDONLY | O_DIRECTORY | O_NOFOLLOW`, and applies `fchown(uid, gid)` then `fchmod(mode)` to
that descriptor. A link at the directory or any parent is status 4. Its first owner caller is the
agent's `.ssh`, which it places through a no-follow walk with the agent as owner, in place of
`install -d -o -g -m`.

## The workstation API

```python
def write(path: str, data: bytes, *, mode: int, parent_mode: int, owner: str | None = None,
          group: str | None = None, elevated: bool = False) -> None
def update(path: str, data: bytes, *, observed: bytes | None, mode: int, parent_mode: int,
           group: str | None = None, preserve_metadata: bool = False,
           elevated: bool = False) -> bool
def ensure_directory(path: str, *, mode: int, owner: str | None = None, group: str | None = None,
                     elevated: bool = False) -> None
```

- **`write`**'s first caller is Phase B's first step, the cloud-init host-key drop-in
  (`vms/initializer/ssh_keys.py:168-171`); `owner`'s is the agent's `authorized_keys`
  (`ssh_keys.py:117-133`).
- **`update`** takes what the caller read as `observed` (`None` for absent). Equal `data` writes
  nothing and returns `False`, so an unchanged file keeps its inode, mode and ACLs (I47). Otherwise
  it calls `write_if` with `expected` the SHA-256 of `observed` (`None` for absent) and returns
  `True`; a change since the read raises `FileConflict` with nothing written, and nothing retries
  (the lead's slice 5 ruling, I37). There is no transform: every caller reads first, because each
  needs the content to decide what to write or what to report. Callers: native settings, the
  zprofile block, the rc source lines, `/etc/fstab`, the sysctl baseline, the tailscaled drop-in,
  apt source lists and the sshd fragment.
- **`ensure_directory`** takes `elevated` now; without `owner` it behaves exactly as slice 5's.

**Errors** are slice 5's (`files-lld.md:400-410`), with no new type: `write` and `ensure_directory`
raise `PathRefused`, `FileLockTimeout` or `CommandFailed`, and `write` also `FileContentMismatch`;
`update` adds `FileConflict`.

`files.py`'s module docstring (`execution/files.py:7-10`) says every mutation is conditional on what
the caller observed; it now names `write` as the unconditional one.

## The dotfiles tree upload

`sources.upload_tree(files, local_dir, dest)` is the one caller's operation, `_fetch_local_dir`'s
(`sources.py:267-289`). It is one unelevated `Script(Shell.SH)` on `files.access`, with
`deadline=files.deadline`, not a file-program operation: it takes no lock and has no `SEALED` entry,
since no conditional writer works beneath a dotfiles checkout.

- **Input.** The workstation builds a gzip `tarfile` archive in memory (`arcname="."`), as
  `copy_dir_to` does on disk today (`transports/base.py:255-261`), and sends it as `Bytes`. The
  destination and a tag, the first 16 hex of the SHA-256 of its last component, travel as the
  environment values `AGW_TREE_DEST` and `AGW_TREE_TAG`, which the log names but never records.
- **The script** is the module constant `UPLOAD_TREE_SCRIPT`, so a test can run it directly. It runs
  under `set -eu`, in order:
  1. Refuse, with status 4, when the destination is a link or something other than a directory.
  2. `mkdir -p` the destination's parent, as today's `mkdir -p` (`transports/base.py:268-271`).
  3. Recover a previous run's leftovers: with `new` and `old` being `<parent>/.agentworks-new-<tag>`
     and `<parent>/.agentworks-old-<tag>`, an `old` with no destination is moved back to the
     destination (a run killed between its renames), any other `old` is removed, and `new` is
     removed.
  4. Set the traps: `trap restore EXIT`, and for each of `HUP`, `INT` and `TERM` a trap that runs
     `restore` and then `exit $((128 + n))` with that signal's number. `restore` removes `new` when
     present, and when `old` is present moves it back if the destination is absent, else removes it.
     It is idempotent, so the `EXIT` trap that follows a signal's `exit` finds nothing to do.
  5. `mkdir -m 0700 new`, then
     `/usr/bin/tar -x -z -f - -C new --no-same-owner --no-same-permissions` reading the archive from
     stdin. A failure here, `tar`'s included, ends the script under `set -e`, and the `EXIT` trap
     removes `new`.
  6. `mv -T` the destination to `old` when it exists, then `mv -T new` to the destination.
  7. Clear the traps, then `rm -rf old || :`: a leftover `old` is harmless, and step 3 of the next
     upload removes it.
- **Not atomic.** Between the two renames the destination is absent. A failure or a signal there
  runs `restore`; a `SIGKILL` or a reboot there leaves `old`, which the next upload to the same
  destination moves back or removes in step 3, before it touches anything. Leftovers therefore need
  no sweep beyond that step and no warning. Two uploads to one destination never overlap, because
  create, reinit and agent initialization hold the VM's native mutation guard (`driver.py:183`,
  `agents/realize.py:117`, `agents/manager/lifecycle.py:504`).
- **Statuses.** 0; 4 is `PathRefused(UNSUITABLE)` naming the destination; anything else is
  `CommandFailed`, whose stderr tail names `tar`'s complaint (a corrupt archive leaves the
  destination as it was, since nothing moves before `tar` succeeds). `upload_tree` is the third
  raiser of `CommandFailed`, beside `VMAccess.run` and `Files`.
- **Links.** The destination itself is never a link, and nothing is extracted outside `new`:
  `--no-same-owner` and the unelevated identity bound what `tar` can create to what the account can.
  The parent path resolves as the shell resolves it; the operation is unelevated, as its one caller
  always is.

## Phase B on `VMAccess`

### Plumbing

- **One access.** `bootstrap_vm` builds the admin's access from the row it re-reads after Phase A
  recorded the address
  (`execution.vm_access(db.get_vm(vm_name), config, Admin(), log=logger.record)`) and returns
  `(access, home, release)` in place of `ts_target` (`driver.py:139-147`). Reinit passes the access
  it already built for the contract (`reinit.py:282`) and drops `ts_target` (`:269`). Phase B uses
  that one access for everything, the git credentials' runup included (`driver.py:555`).
- **One bound.** `vms/initializer/steps.py` defines `STEP_DEADLINE = 60`, today's
  `default_timeout=60` (`driver.py:144`, `reinit.py:269`). Phase B builds
  `files = access.files(deadline=STEP_DEADLINE)` once, and every helper takes that `files`, running
  its commands through `files.access` with `deadline=files.deadline` unless the request has its own
  bound, as slice 5's native CLI does (`files-lld.md:357-367`). `apply_vm_setup` takes the same
  `files` in place of `config` and builds no access of its own, so `VM_SETUP_DEADLINE`
  (`harness_setup/lifecycle.py:48`) goes and one constant states the bound. Agent initialization
  passes `access.files()`, with no bound, as today.
- **No retry.** The legacy transport retries a timed-out command up to its `retries`, and Phase B's
  has one attempt (`ssh.py:336`, `transports/ssh.py:153`), so it never retried. `VMAccess` never
  retries (HLA D5), so nothing changes.
- **What a step catches.**
  `STEP_FAILURES = (ConnectivityError, ExternalError, RequestRefused, FileConflict, PathRefused, FileLockTimeout)`.
  Today's handlers catch `SSHError`, which covered a timeout, a dropped connection and a nonzero
  exit alike (`ssh.py:81-90`); here those are `ExecutionIncomplete` (a `ConnectivityError`) and
  `CommandFailed` (an `ExternalError`), and a missing command or working directory is
  `RequestRefused`. `TrustMissing`, `TrustMismatch`, `VMNotReady` and `PermissionDenied` are not
  step failures: each means every later request fails the same way, so it propagates and fails
  initialization once. Today only `TrustMissing` did (`transports/ssh.py:168-180`); a host-key
  mismatch was ssh's exit 255, an `SSHError` that every remaining step warned about in turn.
  "Catches" below says which steps differ.
- **What a warning says.** `warn_step(log, summary, error)` replaces the 39 copies of
  `msg = f"... failed: {e}"; logger.warning(msg); output.warn(msg)`, and `warn(log, message)`, which
  it calls, writes a warning that carries no error. It writes
  `describe_failure(error, log.sanitize)`: the error's message, and for a `CommandFailed` whose
  request logged its streams (`streams_logged`), a tail of its stderr. The whole stderr is decoded
  and redacted with the log's registered secrets first (`SSHLogger.sanitize`, `ssh.py:194-198`), so
  a secret spanning lines is removed before anything is cut, and then the last three lines are taken
  and made terminal-safe by `ssh_process.tail_lines` (`execution/ssh_process.py:277-286`), as
  `tailscale_join` takes its reason's tail (`capabilities/vm_platform/tailscale_join.py:109`).
  Today's `SSHError` message carried the whole command and stderr (`transports/ssh.py:360`), and
  `output.warn` printed it without redaction; the operation log still holds the full streams,
  redacted (HLA D13). The agent initializer's own steps, which warn on the console only
  (`agents/initializer.py:499`, `:583`, and so on), use `describe_failure` with `output.warn`; the
  helpers it shares with Phase B (the profile and rc fragments and their source lines, the install
  commands, mise) warn through `steps.warn`, into the agent's operation log as well.
- **`streams_logged`.** `CommandFailed` takes the failed request's `log_streams` as a required
  constructor argument; its three raisers, `VMAccess.run(check=True)`, `Files` and `upload_tree`,
  pass it. A request that keeps its streams out of the log keeps them out of a warning too.
- **The log protocol.** `steps.InitLog(OperationLog, Protocol)` adds Phase B's members (`warning`
  and `sanitize`) to Phase A's `OperationLog` (`phase_a.py`), which already has `record`, and which
  stays as it is, and what `run_initialization` reads besides (`has_warnings`, `warnings`) and the
  `redactions` native setup reads; `harness_setup.lifecycle.SetupLog` names what the apply functions
  read (`redactions`, `warning`, `record`), so `harness_setup` does not depend on `vms`. `SSHLogger`
  satisfies both, and the migrated files import nothing from `agentworks.ssh`, so they join the
  static check; `agents/initializer.py` types its log as `InitLog`.

### Shapes

Today's `run(cmd)` runs `$SHELL -c cmd` through sshd as the admin, and `sudo=True` wraps it in
`sudo -n bash -c` (`transports/ssh.py:271-272`). The translation is mechanical:

- A simple command becomes a `Command` with its literal argv, which also removes today's unquoted
  interpolations (`driver.py:588`, `vms/initializer/workspaces_dir.py:57`, `:67`).
- A compound line becomes a `Script` under `Shell.SH`. The one that needs bash, the credential
  reconcile, says `Shell.BASH`.
- `sudo=True` becomes `elevated=True` ("root" below).
- `{shell} -lc '<line>'` stays exactly that, as `Command((shell, "-lc", line))`. It is not
  `Script(..., Shell.USER_DEFAULT, login=True)`, because on create the system install commands run
  before the admin's login shell is set (`driver.py:460-477`, `:512-522`), and the configured shell
  is the contract here. `shell` is an `AccountShell` (`execution/requests.py:23`), so it is one of
  four names. The operator's line is in the argv, so the operation log records it, as today's log
  records the command line; install commands are operator configuration that carries no secret
  (provisioning is hermetic, I77).
- `check=False` stays `check=False`, with the status read from `Result.require_completed`
  (`steps.status(access, request)` runs a request on `access` and returns that status, and a request
  that need only complete is `access.run(request).require_completed(...)`); a completion that is not
  `Completed` raises its typed error, as a transport failure raised `SSHError` under `check=False`
  today.
- An unelevated request sees agw-launch's rebuilt environment and fixed `PATH`
  (`execution/agw_launch.py:59`, `:115-130`) and runs no account startup (I11), where sshd's
  `$SHELL -c` let bash read `~/.bashrc` under `SSH_CLIENT` and zsh `~/.zshenv`. The commands Phase B
  composes itself (`git`, `test`, `cat`, `getent`, `id`, `chmod`, `mkdir`, `curl`, `apt-get`,
  `systemctl` and the like) resolve in that fixed `PATH`. The operator-authored ones (install
  commands, their `test_exec` checks, the dotfiles install) and mise run under `{shell} -lc` or
  `-lic`, which builds the account's login environment.

No request carries an environment but apt's `DEBIAN_FRONTEND=noninteractive`, today's command prefix
(`packages.py:181`, `:209`), now the request's `env`, and the tree upload's two path values (I77).
Stdin is `NoInput` except where the tables say otherwise. "Logged" is the request's `log_streams`; a
file operation's own reads withhold their streams already (`files-lld.md:79-92`). "60" is
`STEP_DEADLINE`.

### What each step catches

Every Phase B step catches `STEP_FAILURES` and warns with `warn_step`, except:

- **Required system packages** (`packages.py:180-184`) catch nothing: their failure stops
  initialization, as today.
- **The DNS check** (`tailscale_dns.py:170-270`) catches `STEP_FAILURES` around its probes and warns
  that DNS could not be checked; today a transport failure there propagated and failed
  initialization. Its own latch `StateError` still raises, deliberately.
- **The mise lockfile** (`mise.py:78-98`) catches `STEP_FAILURES`; today it caught only
  `SourceRefError`, so a failed `mkdir` there failed initialization.
- **Probes whose failure has a meaning**: an install-command check that cannot run means "not
  installed" (`packages.py:290-292`), the `config.toml` probe and the lockfile probe mean "absent"
  (`driver.py:612-624`, `mise.py:144-148`), and `mise prune` is best effort (`mise.py:191-195`).
  Each catches `STEP_FAILURES` and does what today's `except SSHError` did.
- **The dotfiles sync and install** (`driver.py:570-594`) catch `STEP_FAILURES`, where today they
  catch `Exception`. `SourceRefError` is an `ExternalError`, and a local source's `OSError` becomes
  one where it is read (`sources.py`), so no wider catch is needed.
- **`authorized_keys`** (`ssh_keys.py:98-110`) turns every `STEP_FAILURES` error into
  `AuthorizedKeysUnproven`, where today only `SSHError` did (I64).

The agent's phase 1 catches nothing, as today; its phase 2 steps catch `STEP_FAILURES` and warn on
the console, its safe.directory step included, which catches `Exception` today
(`agents/initializer.py:278`). `configure_user_git_credentials` catches `STEP_FAILURES`; the
initializers are its only callers.

### VM Initialization section

| Step                                   | Today                                                                                                   | Request                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | As          | stdin   | Logged    | Deadline |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------- | ------- | --------- | -------- |
| Preserve SSH host keys                 | `mkdir -p` and `printf >` under sudo, `ssh_keys.py:165-171`                                             | `write(SSH_PRESERVE_KEYS_PATH, lines, mode=0o644, parent_mode=0o755, elevated=True)`                                                                                                                                                                                                                                                                                                                                                                                                                         | root        | content | yes       | 60       |
| Mask SVE, gate                         | `ssh_keys.py:216`                                                                                       | `Script(f"{SVE_APPLE_VZ_GREP} && {SVE_CPUINFO_GREP}", Shell.SH)`, `check=False`                                                                                                                                                                                                                                                                                                                                                                                                                              | admin       | none    | yes       | 60       |
| Mask SVE, drop-in                      | `mkdir -p` and `printf >`, `ssh_keys.py:221-227`                                                        | `write(SVE_NOSVE_GRUB_PATH, lines, mode=0o644, parent_mode=0o755, elevated=True)`                                                                                                                                                                                                                                                                                                                                                                                                                            | root        | content | yes       | 60       |
| Mask SVE, apply                        | `ssh_keys.py:228`, `:244`                                                                               | `Command(("update-grub",), elevated=True)`; `Command(("grep", "-qw", "arm64.nosve", "/proc/cmdline"))`; both `check=False`                                                                                                                                                                                                                                                                                                                                                                                   | root, admin | none    | yes       | 60       |
| Sysctl baseline                        | `cat`, compare, `mktemp`, `write_file`, `install -m 0644 -o root -g root`, `rm`, `hardening.py:169-186` | `read(HARDENING_SYSCTL_PATH, elevated=True)`, then `update(..., CONTENT, observed=current, mode=0o644, parent_mode=0o755, elevated=True)`                                                                                                                                                                                                                                                                                                                                                                    | root        | content | no (read) | 60       |
| Sysctl reload                          | `hardening.py:187`                                                                                      | `Command(("sysctl", "-p", HARDENING_SYSCTL_PATH), elevated=True)`, `check=True`, only when `update` wrote; through `steps.update_and_apply`, so a failure puts the prior baseline back                                                                                                                                                                                                                                                                                                                       | root        | none    | yes       | 60       |
| fstab hidepid                          | `cat`, staged `install`, `hardening.py:200-238`                                                         | `read("/etc/fstab", elevated=True)`; `update(..., new_content, observed=current, mode=0o644, parent_mode=0o755, preserve_metadata=True, elevated=True)` (below)                                                                                                                                                                                                                                                                                                                                              | root        | content | no (read) | 60       |
| Remount `/proc`                        | `hardening.py:242`                                                                                      | `Command(("mount", "-o", f"remount,hidepid={effective}", "/proc"), elevated=True)`                                                                                                                                                                                                                                                                                                                                                                                                                           | root        | none    | yes       | 60       |
| DNS check                              | `tailscale_dns.py:206`, `:221`, `:250`                                                                  | `Command(("getent", "hosts", name))`, `Command(("cat", "/etc/resolv.conf"))`, `Command(("systemctl", "is-active", "--quiet", "systemd-resolved"))`; `check=False`                                                                                                                                                                                                                                                                                                                                            | admin       | none    | yes       | 60       |
| tailscaled drop-in                     | `cat`, compare, `install -d`, staged `install`, `rm`, `tailscale_dns.py:105-126`                        | `read(TAILSCALED_DROPIN_PATH, elevated=True)`, then `update(..., CONTENT, observed=current, mode=0o644, parent_mode=0o755, elevated=True)`                                                                                                                                                                                                                                                                                                                                                                   | root        | content | no (read) | 60       |
| tailscaled reload                      | `tailscale_dns.py:127`                                                                                  | `Command(("systemctl", "daemon-reload"), elevated=True)`, `check=True`, only when `update` wrote; through `steps.update_and_apply`, so a failure puts the prior drop-in back                                                                                                                                                                                                                                                                                                                                 | root        | none    | yes       | 60       |
| sshd AcceptEnv                         | `test -f`, `cp` to `.bak`, `tee`, `sshd -t`, restore, `rm`, reload, `shell_env.py:437-458`              | `read(path, elevated=True)` as `prior`; `update(path, body, observed=prior, mode=0o644, parent_mode=0o755, elevated=True)`, and only when it wrote: `Command(("sshd", "-t"), elevated=True)` then `Command(("systemctl", "reload", "ssh"), elevated=True)`, each `check=True`, through `steps.update_and_apply`: any failure after the write restores with `write_if(path, prior, expected=digest(body), ...)` or, with no prior, `remove_if(path, expected=digest(body))` and goes on to the step's warning | root        | content | yes       | 60       |
| sudoers fragments (two)                | `tee` to `.tmp`, `chmod 0440`, `visudo -cf`, `mv`, `rm -f`, `shell_env.py:486-504`                      | `write(path + ".tmp", body, mode=0o440, parent_mode=0o755, elevated=True)`; `Command(("visudo", "-cf", staging), elevated=True)` and `Command(("mv", "-fT", "--", staging, path), elevated=True)`, each `check=True`, so a rejection is the step's warning; in `finally` `Command(("rm", "-f", "--", staging), elevated=True)`, `check=False`                                                                                                                                                                | root        | content | yes       | 60       |
| mise apt key                           | `packages.py:158-162`                                                                                   | `Command(("curl", "-fsSL", MISE_GPG_KEY_URL, "-o", MISE_GPG_KEY_PATH), elevated=True)`                                                                                                                                                                                                                                                                                                                                                                                                                       | root        | none    | yes       | 30       |
| mise apt source                        | `sh -c "printf > ..."` under sudo, `packages.py:163-164`                                                | `write(MISE_SOURCE_FILE, MISE_SOURCE_LINE + "\n", mode=0o644, parent_mode=0o755, elevated=True)`                                                                                                                                                                                                                                                                                                                                                                                                             | root        | content | yes       | 60       |
| apt-get update                         | `packages.py:172`, `:120`                                                                               | `Command(("apt-get", "update", "-qq"), elevated=True)`                                                                                                                                                                                                                                                                                                                                                                                                                                                       | root        | none    | yes       | 120      |
| System packages (fatal)                | `packages.py:180-184`                                                                                   | `Command(("apt-get", "install", "-y", "-qq", "-o", "Dpkg::Options::=--force-confnew", *INIT_SYSTEM_PACKAGES), env={"DEBIAN_FRONTEND": "noninteractive"}, elevated=True)`, run with `check=True`                                                                                                                                                                                                                                                                                                              | root        | none    | yes       | 300      |
| Apt sources, probes                    | `packages.py:50`, `:56`                                                                                 | `Command(("dpkg", "--print-architecture"))`, `Command(("test", "-f", key_path))`; `check=False`                                                                                                                                                                                                                                                                                                                                                                                                              | admin       | none    | yes       | 60       |
| Apt sources, key                       | `packages.py:65`, `:68-83`                                                                              | `Command(("install", "-m", "0755", "-d", key_dir), elevated=True)`; `Script(f"curl -fsSL {q(url)} \| gpg --dearmor -o {q(path)}", Shell.SH, elevated=True)` or `Command(("curl", "-fsSL", url, "-o", path), elevated=True)`; `Command(("chmod", "a+r", path), elevated=True)`                                                                                                                                                                                                                                | root        | none    | yes       | 60       |
| Apt sources, list                      | `cat`, compare, `printf >`, `packages.py:92-110`                                                        | `read(path, elevated=True)`, then `update(path, resolved_source + "\n", observed=current, mode=0o644, parent_mode=0o755, elevated=True)`; an `apt-get update` follows when any wrote                                                                                                                                                                                                                                                                                                                         | root        | content | no (read) | 60       |
| Apt packages                           | `packages.py:208-212`                                                                                   | as system packages, with the template's list, inside the step's handler                                                                                                                                                                                                                                                                                                                                                                                                                                      | root        | none    | yes       | 300      |
| Identity profile                       | `tee`, `shell_env.py:332-334`                                                                           | `write(AGENTWORKS_IDENTITY_PROFILE_PATH, body, mode=0o644, parent_mode=0o755, elevated=True)`                                                                                                                                                                                                                                                                                                                                                                                                                | root        | content | yes       | 60       |
| zprofile identity block                | `mkdir -p`, `test -f`, two `grep -qF`, `sed -i`, `tee -a`, `shell_env.py:348-386`                       | `read(AGENTWORKS_ZPROFILE_PATH, elevated=True)`, warn on one unmatched marker, `update(..., with_identity_block(current, block), observed=current, mode=0o644, parent_mode=0o755, preserve_metadata=True, elevated=True)`                                                                                                                                                                                                                                                                                    | root        | content | no (read) | 60       |
| Skel seeds (two)                       | `tee`, `chmod 644`, `shell_env.py:288-296`                                                              | `write(path, content, mode=0o644, parent_mode=0o755, elevated=True)`                                                                                                                                                                                                                                                                                                                                                                                                                                         | root        | content | yes       | 60       |
| Snap packages                          | `driver.py:454`                                                                                         | `Command(("snap", "install", pkg), elevated=True)`                                                                                                                                                                                                                                                                                                                                                                                                                                                           | root        | none    | yes       | 120      |
| System install commands                | `packages.py:284`, `:297-300`                                                                           | test: `Script(test_cmd, Shell.SH)`, `check=False`; install: `Command((shell, "-lc", entry.command))`                                                                                                                                                                                                                                                                                                                                                                                                         | admin       | none    | yes       | 10; 120  |
| Agent socket root, dirs, stale sockets | `sessions/tmux.py:257-292`, `:310-334`, `:374-410`, called at `driver.py:493-501`                       | the socket helpers' requests (below)                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | root, admin | none    | yes       | 60       |

The fstab step reads first because its messages and the remount need the parsed action
(`hardening.py:226-241`); `update` then writes the content `_ensure_proc_hidepid_in_fstab` returned,
so a `no-op`, `preserved-stricter`, `preserved-unrecognized` or `malformed` action writes nothing,
and an existing file keeps its owner, mode and attributes. Any `hidepid=` value already present
counts as present, and the last one on the line is read, as mount applies it: the values the kernel
accepts, in the kernel's canonical spellings (`0`, `1`, `2`, `4` and the names `off`, `noaccess`,
`invisible`, `ptraceable`), are read as their levels, any other value is reported and left alone,
and the remount uses the value as written, so a value the kernel rejects surfaces as the remount's
warning. A missing `/etc/fstab` (`read` returns `None`) keeps today's warning and skip
(`hardening.py:200-205`).

`with_identity_block(current, block)` is today's logic as a function: an absent file gets the block
alone; with both markers present the old block is stripped and the new one appended; otherwise the
block is appended (`shell_env.py:350-386`), after a newline when the file does not end in one, so
the begin marker starts its own line. The caller warns about one unmatched marker from its own read,
as today (`:370-376`). A missing `/etc/zsh` is created by `write_if`'s parent creation, which runs
exactly when the file is absent, the case `mkdir -p /etc/zsh` existed for (`:343-348`).

The sshd fragment now writes, validates and reloads only when its content differs, so a steady-state
reinit leaves sshd alone (I61); it keeps its prior content on the workstation instead of a `.bak`
beside it, and its rollback is conditional on the fragment still being the one this run wrote.

The sshd fragment, the sysctl baseline and the tailscaled drop-in share one rule, in
`steps.update_and_apply(files, log, path, data, *, observed, mode, parent_mode, apply)`: a changed
file counts as applied only once its follow-up action (`sshd -t` and the reload, `sysctl -p` of the
baseline file alone, `daemon-reload`) succeeds. Any failure after the write (a rejection, a non-zero
status, a timeout or dropped connection, an interrupt) puts the prior content back, or removes a
file the run created, conditional on the file still holding what the run wrote, and then propagates;
the restore runs under `interrupts_deferred`, and an interrupt is re-raised after it. A write whose
own outcome is unknown and may have run (a lost connection, but not a refused key, or an interrupt
during it) gets the same restore. Putting the prior content back is what makes the next reinit
retry, since it sees a difference again.

The restore alone classifies what it finds, and is the one place that warns about it. A
`FileConflict` means the file does not hold what the run wrote: the write never landed, or something
replaced it since; either way the next reinit sees a difference and retries, so nothing more is
reported. Any other step failure leaves the new content in place with nothing applying it, which a
re-run reads as applied and retries nothing; the restore warns, naming the file and saying to put
the prior content back or remove it and reinit. The original failure then propagates as it was: a
step failure is the step's own warning, after the stranded one, and an interrupt, one held through
the restore included, propagates after it.

The baseline is loaded with `sysctl -p` of its own file rather than `sysctl --system`, so another
package's `sysctl.d` key that the running kernel lacks cannot fail the load and undo the baseline.
Every baseline key exists on the supported kernels: Debian's (`CONFIG_SECURITY_YAMA=y`,
`CONFIG_BPF_SYSCALL=y`, checked on 6.1) and WSL2's Microsoft kernel (the same two options in
`config-wsl` and `config-wsl-arm64` on the 6.6 and 6.18 branches); the `fs.protected_*` and
`kernel.*_restrict` keys are core sysctls. A kernel that lacks one fails the load, which restores
the prior baseline and warns on every reinit: check and alert, rather than dropping the key.

The sudoers fragments keep their stage, validate and promote order (`shell_env.py:448-478`): the
staging name contains a `.`, which sudo's `sudoers.d` loader skips, and `mv -T` keeps a directory at
the real path from receiving the fragment inside it. Every path then tries to remove the staging
file; a removal that fails is warned about on its own and never replaces the failure that came
before it, and the `.tmp` file it leaves is one sudo ignores.

The apt key commands and the `/etc/apt/keyrings` directory follow links, as today; that directory,
like every `/etc` path here, can be changed only by root and the admin, who holds sudo.

### Admin Initialization section

| Step                     | Today                                                                  | Request                                                                                                                                                                                                                                                                                                                                                                                                                         | As    | stdin                 | Logged    | Deadline |
| ------------------------ | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- | --------------------- | --------- | -------- |
| Login shell              | `usermod -s $(which shell)`, `driver.py:515-518`                       | `Script(f"path=$(command -v {q(shell)}) && usermod -s \"$path\" {q(admin)}", Shell.SH, elevated=True)`                                                                                                                                                                                                                                                                                                                          | root  | none                  | yes       | 60       |
| Admin home               | `shell_env.py:188`, `:190`                                             | `Command(("chmod", "0750", home))`; `Command(("id", "-gn", admin))`, `check=False`                                                                                                                                                                                                                                                                                                                                              | admin | none                  | yes       | 60       |
| Workspaces parent        | `workspaces_dir.py:57`                                                 | `Command(("mkdir", "-p", "--", workspaces_dir), elevated=True)`                                                                                                                                                                                                                                                                                                                                                                 | root  | none                  | yes       | 60       |
| Workspace ACLs (three)   | `workspaces/acls.py:36-51`                                             | each `find ... -exec setfacl ... {} +` as a literal argv `Command`, `elevated=True`                                                                                                                                                                                                                                                                                                                                             | root  | none                  | yes       | 120      |
| Parent traversal         | `sh -c` loop over `dirname`, `workspaces_dir.py:66-69`                 | `Command(("chmod", "a+x", "--", workspaces_dir, *ancestors), elevated=True)`, the ancestors computed on the workstation, `/` excluded as today                                                                                                                                                                                                                                                                                  | root  | none                  | yes       | 60       |
| mise config              | `mkdir -p`, `write_file`, `mise.py:70-71`                              | `write(f"{home}/.config/mise/config.toml", config, mode=0o644, parent_mode=0o755)`                                                                                                                                                                                                                                                                                                                                              | admin | content               | yes       | 60       |
| safe.directory           | `git_config.py:14`                                                     | `Command(("git", "config", "--global", "--fixed-value", "--replace-all", "safe.directory", "*", "*"))`                                                                                                                                                                                                                                                                                                                          | admin | none                  | yes       | 60       |
| Git credentials          | `reconcile.py:312-317`                                                 | the reconcile `Script` (below)                                                                                                                                                                                                                                                                                                                                                                                                  | admin | secret archive        | (empty)   | 90       |
| Dotfiles, git            | `sources.py:303-331`                                                   | `Command`s: `test -d dest/.git` and `git -C dest config --get remote.origin.url` (`log_streams=False`); else `test -d dest` and `git clone --no-checkout -- url dest` (120); then, every time, `git -C dest fetch -- origin <ref or HEAD>` and `git -C dest checkout --detach FETCH_HEAD` (120 each)                                                                                                                            | admin | none                  | yes       | 60; 120  |
| Dotfiles, local          | `test -d`, `rm -rf`, `copy_dir_to`, `sources.py:282-287`               | `upload_tree(files, local_path, dest)`                                                                                                                                                                                                                                                                                                                                                                                          | admin | archive               | yes       | 60       |
| Dotfiles install         | `cd dest && cmd`, `driver.py:587-590`                                  | `Command((shell, "-lc", cmd), cwd=dest)`                                                                                                                                                                                                                                                                                                                                                                                        | admin | none                  | yes       | 120      |
| mise lockfile, local     | `mkdir -p`, `copy_to`, `mise.py:93`, `sources.py:192`                  | `write(dest, data, mode=0o644, parent_mode=0o755)`                                                                                                                                                                                                                                                                                                                                                                              | admin | content               | yes       | 60       |
| mise lockfile, git       | `mkdir -p`, `sources.py:207-234`                                       | `Command`s `mktemp -d /var/tmp/agentworks-source-ref-XXXXXX`, `git init -q -- tmp`, `git -C tmp remote add origin -- url`, `git -C tmp fetch --depth 1 -- origin <ref or HEAD>` and `git -C tmp checkout --detach FETCH_HEAD` (60); `read(f"{tmp}/{subpath}", limit=16 MiB)`; `write(dest, data, mode=0o644, parent_mode=0o755)`; `Command(("rm", "-rf", "--", tmp))`, its completion and status read and a failure only warned | admin | content               | yes       | 60       |
| mise install             | `mise.py:145`, `:155-158`, `:176-179`, `:195`; `driver.py:613`         | `Command(("test", "-f", path))`, `check=False`; `Command((shell, "-lc", "mise install -y --locked"))` (300), `Command((shell, "-lc", "mise install -y"))` (300), `Command((shell, "-lc", "mise prune -y"))` (60, failures ignored)                                                                                                                                                                                              | admin | none                  | yes       | 60; 300  |
| User install commands    | `packages.py:284`, `:297-300`                                          | as system install commands                                                                                                                                                                                                                                                                                                                                                                                                      | admin | none                  | yes       | 10; 120  |
| Profile fragment         | `write_file`, `shell_env.py:142`                                       | `write(f"{home}/.agentworks-profile.sh", content, mode=0o644, parent_mode=0o755)`                                                                                                                                                                                                                                                                                                                                               | admin | content               | yes       | 60       |
| Rc fragment              | `write_file`, `shell_env.py:603`                                       | `write(f"{home}/.agentworks-rc.sh", content, mode=0o644, parent_mode=0o755)`                                                                                                                                                                                                                                                                                                                                                    | admin | content               | yes       | 60       |
| Source lines             | grep-or-append, `shell_env.py:146-149`, `:607-610`, `:61-79`           | `ensure_source_line(files, rc, marker, line, log)` for each file `source_files` names (below)                                                                                                                                                                                                                                                                                                                                   | admin | content               | no (read) | 60       |
| `authorized_keys` (last) | `write_file(mode="600")`, `ssh_keys.py:101`, called at `driver.py:223` | `write(f"{home}/.ssh/authorized_keys", content, mode=0o600, parent_mode=0o700)`                                                                                                                                                                                                                                                                                                                                                 | admin | content (public keys) | yes       | 60       |

Harness setup runs between the source lines and `authorized_keys` (`driver.py:205-223`), on the same
access and bound since slice 5.

- **mise failures.** `_parse_mise_failures` reads a `CommandFailed`'s `result.stderr` instead of
  `str(e)` (`mise.py:101-117`), since the new error's message holds no stream.
- **Dotfiles install.** Admin and agent run the operator's `dotfiles_install_cmd` the same way, as
  `{shell} -lc` in the checkout, which the agent already does (`agents/initializer.py:368-372`). The
  admin's ran under sshd's non-login `$SHELL -c`; it now gets the login environment the agent's had,
  and `cwd` replaces the unquoted `cd {dest} &&`.
- **The mise lockfile** from git is read back and written by the file program, so both sources
  publish it the same way, refusing a link at the destination, at 0644. The `mkdir -p` goes, since
  `write` creates missing parents. The 16 MiB read limit is far above any `mise.lock`.
- **Local dotfiles** were logged as `logger.warning(f"overwriting existing {dest}")`
  (`sources.py:282-284`), which marks every admin reinit with a local dotfiles directory partial
  (`driver.py:256`). The replacement is expected, so it is a `logger.output` line (decision 4).
- **Git destinations** keep `git`'s own handling: `test -d`, `git -C`, `fetch`, `checkout` and
  `clone` follow a link at the destination, as today.
- **One dotfiles sync.** `vms/initializer/dotfiles.py`'s `sync_dotfiles` is both accounts' sync and
  install, so the agent's own git handling (`agents/initializer.py:316-355`) goes. A fresh clone
  takes `--no-checkout` and no `--branch`; then, fresh or existing, the ref (or `HEAD` without one)
  is fetched from origin and `FETCH_HEAD` checked out detached, so a changed or dropped ref takes
  effect, a branch advances and a tag or a full commit hash works (the admin's `pull` ignored the
  ref; the agent's `checkout ref` did not advance a branch; `clone --branch` refused a hash). The
  git file source fetches the same way at depth 1 into a fresh repository. An existing clone's
  origin is read with `log_streams=False` and a mismatch names neither URL, since an origin cloned
  with credentials holds them. Every git command's status is read, and a failed sync skips the
  install for both accounts (I68), where the agent's warned and installed anyway.
  `sources.DOTFILES_DEADLINE` (120) bounds each git command and the install.
- **The authorized-keys proof.** Any `STEP_FAILURES` error from the `write` becomes
  `AuthorizedKeysUnproven`, as `SSHError` did (`ssh_keys.py:100-110`); the checkpoint logic that
  follows is unchanged (`driver.py:230-288`).

### Dotfiles destinations

`dotfiles_destination` is operator configuration expanded today with `.replace("~", home)`, which
rewrites every `~` in it (`driver.py:568`, `agents/initializer.py:312`).
`sources.normalize_destination` becomes its validator on both templates (`vms/admin.py:122`,
`agents/template.py:122`), at template load, stored overlays included: the value is `~/` followed by
a relative path, a bare relative path (normalized to `~/` and the path, which is what it always
meant, so a stored `dots` keeps working), or an absolute path; it has no empty, `.` or `..`
component and no other user's `~`; a trailing `/` is dropped; and `~` or `~/` alone, the home
itself, is refused, since replacing it would move the home aside. A path in the home that is, holds
or lies inside one Agentworks manages in that account's home is refused too. `vms/home_layout.py`
holds a set per account, which `normalize_destination` and `expand_destination` take as `managed`:
`MANAGED` (the two fragments, `.ssh`, the state directory `.agentworks` and `.agentworks-artifacts`)
for an agent, and `ADMIN_MANAGED`, which adds the workspace links' `.config/tmuxinator`, for the
admin, so an admin's `~/.config` is refused and an agent's is not. `.config/mise` is in neither:
dotfiles may own or override the mise config, since both accounts write it before the dotfiles sync
and fetch the lockfile and install after it (`docs/guides/mise.md`). Refusals are
`DestinationError`, an `errors.ValueFreeError` whose message names the accepted forms and no part of
the value, which `instance_overlay_codec` shows in place of its generic message.
`expand_destination(value, home)` replaces only the leading `~` and refuses, before any request, an
absolute value that is the home or one of its ancestors, or that lands on a managed path.

### Source lines

`ensure_source_line(files, rc, marker, line, log)` replaces the seven grep-or-append strings
(`shell_env.py:66-69`, `:76-79`, `:147-149`, `:608-610`; `agents/initializer.py:579-581`,
`:612-614`, `:660-663`). It reads `rc`; when `marker` occurs in it (today's `grep -q`) it is done;
otherwise it calls `update` with `line` appended, first adding a newline when the file does not end
with one (today's `printf >>` joins the line to a final unterminated one), `observed` the read,
`mode=0o644`, `parent_mode=0o755` and `preserve_metadata=True`. A missing file is created 0644, as
`>>` under the default umask creates it. It returns nothing; the one outcome worth noting, a linked
file whose target already sources the fragment, it logs itself (below). Every writer appends the
same line, `. <home>/<fragment>`, with the fragment's file name as the marker, so a line written by
any earlier version still satisfies it.

`source_files(home, shell)`, a mapping from each fragment's file name to its rc files, names the
files once for every writer: the profile line goes to `.profile` and `.bashrc`, and `.zprofile` for
a zsh account; the rc line to `.bashrc`, and `.zshrc` for a zsh account. These are
`_ensure_agentworks_files_sourced`'s lists (`shell_env.py:61-79`). Today `_write_agentworks_profile`
writes `.profile` and `.zprofile` whatever the shell (`shell_env.py:146-149`) and both rc writers
write `.bashrc` and `.zshrc` whatever the shell (`shell_env.py:607-610`,
`agents/initializer.py:611-614`), so a bash account no longer gets a `.zprofile` or `.zshrc` created
for it.

**A linked rc file** is never written through (decision 3). `read` refuses a link, or any other type
that is not a regular file, with `PathRefused(UNSUITABLE)`; `ensure_source_line` then asks which it
was with `Command(("test", "-L", rc))`. Only for a link does it run
`Command(("grep", "-qsF", "--", marker, rc))` as the same identity, which reads through the link:
when the linked file holds the marker, the step is satisfied and logs a line; otherwise it warns
that `rc` is a link, naming the line to add to the dotfiles. Any other type gets the ordinary step
warning with the refusal's own message.

**A linked mise config** is not written either. When the config's `write` is refused as unsuitable,
the step warns in its own words: the mise config at that path was not written because a link or
another file is at it or its directory, which dotfiles usually own, so `mise_packages` does not
reach it until the template drops `mise_packages` or the dotfiles drop the link.

Today's `_ensure_agentworks_files_sourced` runs its appends with `check=False`, so a failed append
was silent (`shell_env.py:66-79`); each is now a step failure that warns.

## Git credentials

`configure_user_git_credentials(access, requests, config, logger, *, target_role)` loses its
`target` (`git_credentials/__init__.py:146-172`); the runup already used `access` (slice 5). The
reconcile becomes one request on that same access, so it runs as the user whose credentials it
converges, admin or agent:

```python
Script(_RECONCILE_SCRIPT.replace("@DESIRED@", desired), Shell.BASH,
       stdin=Bytes(archive), deadline=90)
```

run with stdout and stderr `DiscardOutput()` and `check=False`.

- **Secret handling.** The archive holds the credential material (`reconcile.py:97-116`). It is the
  request's stdin, which no log record, error or `repr` holds (`files-lld.md:86-92`, vector F1).
  Today's `input_text` discarded the command's output because a command can reflect its stdin
  (`transports/ssh.py:255-258`); `DiscardOutput` on both streams keeps that, and since a discarded
  stream reaches the result empty, the operation log has nothing to write for it
  (`execution/oplog.py:36-41`), so `log_streams=False` would add nothing and is not set. The
  script's exit status is the whole answer, mapped by `_RECONCILE_FAILURES` (`reconcile.py:291-302`)
  to value-free messages, raised as `ExternalError`. A completion that is not `Completed` raises its
  typed error, whose message holds no stream (HLA D13), in place of today's replaced message
  (`reconcile.py:318-319`). The resolved credentials are in the operation log's redaction set from
  its construction (`vms/manager/reinit.py:257-268`, `agents/realize.py:117-124`), I28.
- **The archive as bytes.** `input_text` was text, which is why the archive is base64 and the script
  runs `base64 -d | tar -xzf -` (`reconcile.py:116`, `:253`). `Bytes` carries any byte exactly
  (vector F2), so `_state_archive` returns the gzip bytes and the script runs `tar -xzf -` (decision
  6).
- **Always bash.** Today the script runs under the account's login shell through sshd. It uses
  `set -E` and `trap 'exit 29' ERR` (`reconcile.py:119-120`); under dash, which is Debian's `sh`,
  the trap is refused (`dash -c 'trap "exit 29" ERR'` prints `trap: ERR: bad trap` and exits 1,
  measured on the baseline host), so for an `sh` or `dash` account every reconcile fails as
  "unexpectedly", and `dbb7e0ac9` had to rename a variable to keep it safe under zsh. `Shell.BASH`
  runs it under `/bin/bash` whatever the account's shell, with no startup file (I11).
- `configure_user_git_credentials` catches `STEP_FAILURES` and warns, where it caught `SSHError`
  (`git_credentials/__init__.py:165-171`).

## The agent initializer and grants

ADR 0011's split holds: the admin bootstraps the account, then the agent configures itself over its
own access (`agents/initializer.py:89-105`). `create_agent_on_vm` builds
`admin = execution.vm_access(vm, config, Admin(), log=logger.record)` and, once `authorized_keys` is
in place, `agent = execution.vm_access(vm, config, Agent(agent_name, linux_user), log=...)`, each
with `files()` and no bound, as today's factories had none. The agent's access refuses any elevated
request before dispatch (D12), so the self-configure phase cannot regress into root work.

| Step                            | Today                                                               | Request                                                                                                                                                                                     | As          | stdin                 | Deadline  |
| ------------------------------- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- | --------------------- | --------- |
| Fresh user, refusals            | `agents/initializer.py:55-61`                                       | `Command(("getent", "passwd", user), elevated=True)`; `Script(f"test -e {q(home)} \|\| test -L {q(home)}", Shell.SH, elevated=True)`; `check=False`                                         | root        | none                  | none      |
| Fresh user                      | `:68`                                                               | `Command(("useradd", "-m", "-U", "-s", f"/bin/{shell}", user), elevated=True)`                                                                                                              | root        | none                  | none      |
| Converge user                   | `:151`, `:153`, `:162`                                              | `getent passwd` (`check=False`), `useradd -m -U -s`, `usermod -s`, each a `Command`, `elevated=True`                                                                                        | root        | none                  | none      |
| Home mode, group check          | `:178`, `:188`                                                      | `Command(("chmod", "0750", home), elevated=True)`; `Command(("id", "-gn", user), elevated=True)`, `check=False`                                                                             | root        | none                  | none      |
| Socket root, dir, stale sockets | `:205-207`                                                          | the socket helpers on the admin's `files`                                                                                                                                                   | root, admin | none                  | none      |
| `.ssh` directory                | `install -d`, `ssh_keys.py:117-120`                                 | `ensure_directory(f"{home}/.ssh", mode=0o700, owner=user, group=user, elevated=True)`                                                                                                       | root        | none                  | none      |
| `authorized_keys`               | `mktemp`, `write_file`, `sudo install`, `rm`, `ssh_keys.py:121-135` | `write(f"{home}/.ssh/authorized_keys", content, mode=0o600, owner=user, group=user, parent_mode=0o700, elevated=True)`                                                                      | root        | content (public keys) | none      |
| Profile fragment (twice)        | `write_file(mode="0644")`, `:573`                                   | `write(f"{home}/.agentworks-profile.sh", content, mode=0o644, parent_mode=0o755)`                                                                                                           | agent       | content               | none      |
| Rc fragment                     | `write_file(mode="0644")`, `:609`                                   | `write(f"{home}/.agentworks-rc.sh", content, mode=0o644, parent_mode=0o755)`                                                                                                                | agent       | content               | none      |
| Source lines                    | `:579-581`, `:612-614`, `:660-663`; `shell_env.py:61-79`            | `ensure_source_line` on the agent's `files`, over `source_files`                                                                                                                            | agent       | content               | none      |
| safe.directory                  | `:276`                                                              | as the admin's, on the agent's access                                                                                                                                                       | agent       | none                  | none      |
| Git credentials                 | `:285-292`                                                          | the reconcile `Script` on the agent's access                                                                                                                                                | agent       | secret archive        | 90        |
| Install commands                | `:481`, `:494-497`                                                  | `Script(test_cmd, Shell.SH)` (`check=False`, 10); `Command((shell, "-lc", entry.command))` (120)                                                                                            | agent       | none                  | 10; 120   |
| Dotfiles, git                   | `:316-355`                                                          | `sources.fetch_dir`, as the admin's                                                                                                                                                         | agent       | none                  | none; 120 |
| Dotfiles, local                 | `fetch_dir`, `:360`                                                 | `upload_tree` through `fetch_dir`                                                                                                                                                           | agent       | archive               | none      |
| Dotfiles install                | `{shell} -lc 'cd dest && cmd'`, `:368-372`                          | `Command((shell, "-lc", cmd), cwd=dest)`, as the admin's                                                                                                                                    | agent       | none                  | 120       |
| mise shims `PATH`               | `printf >> profile`, `:652-665`                                     | folded into the profile fragment's content (below); no request                                                                                                                              | n/a         | n/a                   | n/a       |
| mise config, lockfile           | `mkdir -p`, `write_file`, `fetch_file`, `:685-701`                  | `write(config.toml, mode=0o644, parent_mode=0o755)`; `fetch_file` as the admin's                                                                                                            | agent       | content               | none      |
| mise install, prune             | `:708`, `:716-719`, `:726-729`, `:746-749`                          | as the admin's                                                                                                                                                                              | agent       | none                  | 300; 60   |
| Delete                          | `pkill -u`, `rm -rf` socket dir, `userdel -r`, `:413-425`           | three `Command`s, `elevated=True`; the first two read no status; the socket directory still goes before `userdel` (I74)                                                                     | root        | none                  | none      |
| Grant: add                      | `grants.py:256-257`                                                 | `Script(f"getent group {q(g)} >/dev/null 2>&1 \|\| /usr/sbin/groupadd {q(g)}", Shell.SH, elevated=True)`; `Command(("usermod", "-aG", g, user), elevated=True)`; both run with `check=True` | root        | none                  | none      |
| Grant: remove                   | `grants.py:272`                                                     | `Command(("gpasswd", "-d", user, g), elevated=True)`, status not read, a completion required                                                                                                | root        | none                  | none      |

- **Shared with the admin.** The agent writes its profile and rc fragments with Phase B's
  `_write_agentworks_profile` and `_write_agentworks_rc` (passing its identity exports), runs its
  user install commands through Phase B's `_run_install_commands`, whose completion test runs
  `test_exec` as `{shell} -lic` where the agent's used `-lc`, and sets up mise with Phase B's
  `_write_mise_config`, `_fetch_mise_lockfile` and `_run_mise_install`, so a failed config write no
  longer skips the install. One `profile_fragment` builds both accounts' profile, keeping the last
  of interleaved `PATH` entries (I70) for the admin too.
- **The mise shims line.** Today `_run_agent_mise_setup` appends
  `export PATH="<home>/.local/share/mise/shims:$PATH"` to the profile fragment after
  `_run_agent_install_commands` rewrote it (`agents/initializer.py:652-658`), and adds source lines
  to `~/.profile` and `~/.zprofile` whatever the shell (`:659-663`). The fragment is
  Agentworks-owned and rewritten every time, so `_run_agent_install_commands` writes it with the
  shims path last when mise is configured, which keeps its precedence (each export prepends,
  `:507-512`), and the extra source lines go, since `source_files` covers the files the agent's
  shell reads.
- **Grants** keep their signatures (`grants.py:243-273`): each builds the admin's access from the
  `vm` and `config` it already takes, with `log=logger.record` when it has a logger. No caller
  catches `SSHError` by name: each either catches `Exception` (`grants.py:328`, `:346`;
  `sessions/nodes.py:299`) or lets the error propagate, and does the same with `CommandFailed` and
  `ExecutionIncomplete`. `grants.py` stays off the static check, because `revoke_workspace_grants`
  constructs an `SSHLogger` until slice 8 moves it (`grants.py:215`).
- **Phase 1 failures** raise, as today (`agents/initializer.py:83-106`); phase 2's warn on the
  console with `describe_failure`.

## The socket helpers and workspace ACLs

`ensure_agent_socket_root`, `ensure_agent_socket_dir` and `ensure_admin_socket_root` keep their
probes and fast paths (`sessions/tmux.py:240-334`, `:416-444`); each probe is an elevated
`Script(Shell.SH)` with `check=False`, and each `mkdir`, `chown`, `chmod`, `groupadd` and `usermod`
an elevated `Command` (`getent group` is an unelevated `Command` with `DiscardOutput`). They take
`files: Files`, so Phase B bounds them at 60 seconds and agent and session create leave them
unbounded, as today.

`cleanup_stale_sockets` keeps its framed inventory and its per-socket guard
(`sessions/tmux.py:367-413`, `4a64820d9`): the inventory is an elevated `Script(Shell.SH)` and each
removal an elevated `Script` with the socket guard. The server probe runs as the socket's owner:
`_cleanup_stale_sockets_under` takes that owner, the agent's Linux user, and its per-socket `run`
wraps the probe's line as `runuser -u <owner> -- sh -c '<line>'` inside the guard, so `tmux` runs as
the agent, and root runs only the guard and the guarded `rm -f` (`sessions/tmux.py:400-410`, where
today the probe runs elevated). In this slice's code that probe is the only `tmux` client that runs
against an agent's socket as another account; session commands run as the session's owner, and the
session paths are slice 7's. Its docstring (`:338-345`) loses the reference to the deleted admin
twin and the double-dash aside. `probe_tmux_server` takes a `RunCommand` (`sessions/tmux.py:71-90`,
`:1060-1068`), so `access_run_command(access, *, deadline, elevated)` adapts one: it runs the line
as a `Script(Shell.SH)` with the given `env` and returns the decoded `stdout`, `stderr`,
`returncode` and `ok` the probe reads, raising the typed error for a completion that is not
`Completed`, as `SSHError` was raised. It is the tmux probes' one adapter: slice 7 moves every other
`RunCommand` user onto it. It lives in `sessions/tmux.py` beside the protocol.

`cleanup_stale_admin_sockets` (`sessions/tmux.py:353-365`) has had no caller since it was added in
`ddf9973b9`, though its docstring says reinit calls it; it is deleted rather than ported (decision
5).

`tmux.create_session` uses its `target` only for the socket roots (`sessions/tmux.py:868-891`), so
it takes `admin_files: Files` in its place; its two callers (`sessions/manager/_create_roll.py:426`,
`sessions/manager/_lifecycle.py:1066`) build it from the rows they hold.

`apply_workspace_acls(files, path)` runs its three `find ... -exec setfacl` lines as literal argv
`Command`s, elevated, each with today's 120 seconds (`workspaces/acls.py:32-51`), the clone-attempt
prune included (`78011cce9`); `workspace_acl_commands(path)` builds them. Its four slice 7 callers
(`workspaces/backends/vm.py:194`, `workspaces/manager/repair.py:217`, `rehome.py:259`,
`copy.py:203`) build an admin access beside their target with `files()`, as slice 5 did for the
clone cleanup (`workspaces/backends/vm.py:212`). Repair and rehome, which warn on an ACL failure and
go on, catch `CommandFailed`, `ExecutionIncomplete` and `RequestRefused` beside the `SSHError` their
legacy steps still raise.

## Root operations on account-owned paths

How each elevated request in this slice that touches a path an unelevated account owns is placed.

| Operation                                                                                          | Path                                                                                                                                          | Design                                                                                                        |
| -------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| The agent's `.ssh` (`ssh_keys.py:117-120`)                                                         | inside the agent's home                                                                                                                       | `ensure_directory` with `owner`: a no-follow walk, then `fchown` and `fchmod` on a no-follow descriptor (I79) |
| The agent's `authorized_keys` (`ssh_keys.py:121-135`)                                              | inside the agent's `.ssh`                                                                                                                     | `write`: a no-follow walk, then a rename of a file already given its owner and mode (I79)                     |
| `chmod 0750` and `id -gn` on the agent's home (`agents/initializer.py:178`, `:188`)                | the home's entry is in root-owned `/home`                                                                                                     | a `Command` on that entry                                                                                     |
| Fresh-user `test -e` and `test -L` (`agents/initializer.py:56-60`)                                 | `/home`, root-owned                                                                                                                           | read-only; the result only refuses                                                                            |
| Agent socket directory `mkdir`, `chown`, `chmod` (`sessions/tmux.py:332-334`)                      | its entry is in `/run/agentworks/agent-tmux-sockets`, `root:tmux-agent-access` 2771, whose group holds only the admin (`tmux.py:266`, `:289`) | `Command`s on that entry                                                                                      |
| Stale-socket probe and removal (`sessions/tmux.py:374-410`)                                        | inside the agent-owned socket directory                                                                                                       | the probe runs as the socket's owner through `runuser`; root runs the link-refusing guard and `rm -f` (above) |
| Agent delete's `rm -rf` of the socket directory and `userdel -r` (`agents/initializer.py:413-425`) | the directory's entry is root-and-admin owned                                                                                                 | `rm -rf` and `userdel`, as today                                                                              |
| Workspace ACLs (`workspaces/acls.py:36-51`)                                                        | workspace trees                                                                                                                               | today's mechanism, below                                                                                      |
| Every `/etc`, `/run`, `/proc` and workspaces-parent operation                                      | root-owned paths, which only root and the admin (who holds sudo) can change                                                                   | as each table above says                                                                                      |

Workspace ACL application keeps today's mechanism in this slice; its hardening is left to the
permissions effort.

## Every hand-written write this slice replaces

Mode, owner and group are what the new request sets; "today" is what the old sequence produced.
Every one of these sends its content on stdin, and none is secret: the only secret this slice moves
is the git credential archive, which is a `Script`'s stdin (above), never a file operation's
argument or a logged stream.

| Call site                                                              | Today                                                                | New                                                             | Mode                  | Owner:group    | Secret |
| ---------------------------------------------------------------------- | -------------------------------------------------------------------- | --------------------------------------------------------------- | --------------------- | -------------- | ------ |
| `vms/initializer/ssh_keys.py:168-171` cloud-init drop-in               | `printf >` as root                                                   | `write`, elevated                                               | 0644                  | root:root      | no     |
| `ssh_keys.py:224-227` grub `arm64.nosve` drop-in                       | `printf >` as root                                                   | `write`, elevated                                               | 0644                  | root:root      | no     |
| `ssh_keys.py:101` admin `authorized_keys`                              | `write_file` then `chmod 600`                                        | `write`                                                         | 0600                  | admin's        | no     |
| `ssh_keys.py:117-120` agent `.ssh`                                     | `sudo install -d -o -g -m 0700`                                      | `ensure_directory`, elevated, `owner`                           | 0700                  | agent:agent    | no     |
| `ssh_keys.py:121-135` agent `authorized_keys`                          | `mktemp`, `write_file`, `sudo install -o -g -m 0600`, `rm`           | `write`, elevated, `owner`                                      | 0600                  | agent:agent    | no     |
| `vms/hardening.py:175-186` sysctl baseline                             | `mktemp`, `write_file`, `sudo install -m 0644 -o root -g root`, `rm` | `update`, elevated                                              | 0644                  | root:root      | no     |
| `hardening.py:227-238` `/etc/fstab`                                    | the same staging, which reset it to 0644 root:root                   | `update`, elevated, `preserve_metadata`                         | kept                  | kept           | no     |
| `vms/tailscale_dns.py:115-126` tailscaled drop-in                      | the same staging                                                     | `update`, elevated                                              | 0644                  | root:root      | no     |
| `vms/initializer/shell_env.py:332-334` identity profile                | `printf \| sudo tee`                                                 | `write`, elevated                                               | 0644                  | root:root      | no     |
| `shell_env.py:384-386` zprofile block                                  | `printf \| sudo tee -a` after `sed -i`                               | `update`, elevated, `preserve_metadata`                         | kept (0644 if absent) | kept           | no     |
| `shell_env.py:293-296` skel `.bashrc` and `.zshrc`                     | `printf \| sudo tee`, `sudo chmod 644`                               | `write`, elevated                                               | 0644                  | root:root      | no     |
| `shell_env.py:441-450` sshd AcceptEnv fragment                         | `cp` to `.bak`, `printf \| sudo tee`, `mv` or `rm` back              | `update`, then `write_if` or `remove_if` on rejection, elevated | 0644                  | root:root      | no     |
| `shell_env.py:491-498` sudoers staging (two fragments)                 | `printf \| sudo tee`, `sudo chmod 0440`                              | `write`, elevated                                               | 0440                  | root:root      | no     |
| `shell_env.py:142` admin `~/.agentworks-profile.sh`                    | `write_file`, no mode                                                | `write`                                                         | 0644                  | admin's        | no     |
| `shell_env.py:603` admin `~/.agentworks-rc.sh`                         | `write_file`, no mode                                                | `write`                                                         | 0644                  | admin's        | no     |
| `shell_env.py:66-79`, `:147-149`, `:608-610` admin rc lines            | `grep -q \|\| printf >>`                                             | `ensure_source_line` (`update`, `preserve_metadata`)            | kept (0644 if absent) | kept           | no     |
| `vms/initializer/packages.py:107-110` apt source list                  | `printf >` as root                                                   | `update`, elevated                                              | 0644                  | root:root      | no     |
| `packages.py:163-164` mise apt source                                  | `printf >` as root                                                   | `write`, elevated                                               | 0644                  | root:root      | no     |
| `vms/initializer/mise.py:70-71` admin mise config                      | `mkdir -p`, `write_file`                                             | `write`                                                         | 0644                  | admin's        | no     |
| `sources.py:192` local file source (mise lockfile)                     | `copy_to` (scp)                                                      | `write`                                                         | 0644                  | the identity's | no     |
| `sources.py:225-226` git file source (mise lockfile)                   | `cp` on the guest                                                    | `read` of the clone, then `write`                               | 0644                  | the identity's | no     |
| `sources.py:282-287` local directory source (dotfiles)                 | `rm -rf`, `copy_dir_to`                                              | `upload_tree`, a `Script`                                       | `tar`'s, under umask  | the identity's | no     |
| `agents/initializer.py:573` agent profile fragment                     | `write_file(mode="0644")`                                            | `write`                                                         | 0644                  | agent's        | no     |
| `agents/initializer.py:609` agent rc fragment                          | `write_file(mode="0644")`                                            | `write`                                                         | 0644                  | agent's        | no     |
| `agents/initializer.py:579-581`, `:612-614`, `:660-663` agent rc lines | `grep -q \|\| printf >>`                                             | `ensure_source_line`                                            | kept (0644 if absent) | kept           | no     |
| `agents/initializer.py:656-658` agent shims `PATH`                     | `printf >> profile`                                                  | part of the profile's content                                   | n/a                   | n/a            | no     |
| `agents/initializer.py:685-686` agent mise config                      | `mkdir -p`, `write_file(mode="0644")`                                | `write`                                                         | 0644                  | agent's        | no     |
| `agents/initializer.py:701` agent mise lockfile                        | `fetch_file`                                                         | `fetch_file`, as the admin's                                    | 0644                  | agent's        | no     |

**Modes that change.** The admin's profile, rc and mise config passed no mode (`shell_env.py:142`,
`:603`, `mise.py:71`), so a new file took whatever mode the scp of a workstation temporary file gave
it; they now get 0644, the mode their agent twins already set (`agents/initializer.py:573`, `:609`,
`:686`). A mise lockfile took its workstation file's mode through scp, or the clone's through `cp`;
from either source it is now 0644. `/etc/fstab` was reset to 0644 `root:root` by its staged
`install`; it now keeps its metadata.

## Behavior changes for the upgrade guide

- **What is refused at a link.** Initialization never writes through a symbolic link at, or above,
  the files the file program writes: the profile and rc fragments and the rc files that source them
  (a linked rc file is refused, and accepted only when it already holds the line), the mise config
  and lockfile, `authorized_keys` and the agent's `.ssh`, and the `/etc` files. A local dotfiles
  destination that is itself a link is refused. Git checkouts and the apt key downloads follow
  links, as before.
- **Previously silent failures now warn.** A source-line append that failed was ignored; it now
  warns and leaves initialization partial. Under decision 3 that includes a linked rc file that
  lacks the line: the Agentworks fragments are then not sourced, so `AGENTWORKS_AGENT` and the
  install commands' `PATH` entries are missing from that account's login shells, and every reinit
  stays partial until the line is in the dotfiles. A linked mise config is not written either, with
  the same warning and status.
- A DNS check that cannot reach the VM warns instead of stopping initialization, and so does a mise
  lockfile step that cannot create its directory.
- A git-sourced `mise.lock` that is a symbolic link in its repository is refused with a warning.
- A host-key mismatch stops initialization at the first request, as a missing record already did,
  instead of every remaining step warning.
- Step warnings end with the failed command's last stderr lines, redacted; the full output stays in
  the operation log.
- The admin's profile, rc and mise config files and the mise lockfile are 0644; `/etc/fstab` keeps
  its owner, mode and attributes where it was reset to 0644 `root:root`.
- The admin's `dotfiles_install_cmd` runs in a login shell, as the agent's always has, with
  agw-launch's environment and fixed `PATH` underneath it instead of sshd's non-login shell.
- A local dotfiles directory is replaced through a temporary sibling; the previous tree comes back
  if the upload fails or is interrupted, and the replacement no longer marks the reinit partial.
- `dotfiles_destination` is a path in the home (`dots` or `~/dots`) or an absolute path; it may not
  name the home itself, be, hold or lie inside a path Agentworks manages there, or use `..`, and
  loses a trailing `/`; only its leading `~` is expanded.
- Git credentials reconcile under bash for every account, so an `sh` or `dash` admin or agent gets
  its credentials.
- A bash account no longer gets a `~/.zprofile` or `~/.zshrc` created for it.
- The sshd fragment leaves no `.bak` file and reloads sshd only when it changed.
- A missing login shell fails "Shell configuration" with a warning, where `$(which ...)` gave
  `usermod -s` an empty argument (`driver.py:516`).

## Tests

None restates the implementation or our prose (`no-prose-policing-tests`).

**The plan's Tests box:**

| Plan item                                                                                                    | Test                                                                                                                                                                                                                                                                                                                                                                                                                |
| ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `write` applies mode and owner before the rename                                                             | `test_file_program.py` runs `main` in-process with `os.rename` wrapped to `fstat` the source name relative to its directory before delegating: at that moment the temporary file has the requested mode and group, a set-gid mode included (which chown before chmod keeps); under the sudo lane, the requested owner too. The destination afterwards has them, with no `.agentworks-tmp-*` left                    |
| a failed or interrupted tree upload leaves the previous tree in place, and a corrupt archive changes nothing | Over the L lane, `upload_tree` with a truncated archive leaves the destination byte-identical with no `.agentworks-*` sibling; `UPLOAD_TREE_SCRIPT` run directly with a shadowed `mv` that sends the script `SIGTERM` after the first rename restores the old tree and exits 143; a sibling `.agentworks-old-<tag>` with no destination, as a `SIGKILL` leaves, is moved back by the next upload before it extracts |

**The file program, against a real local filesystem** (`cli/tests/execution/test_file_program.py`,
Linux CI, both interpreters, as slice 5's tests run):

- `write` replaces an existing file whatever it holds, drops its old mode and ACLs, refuses a link
  at the destination or a parent (status 4) leaving the link's target untouched, and creates missing
  parents with `parent_mode`; a truncated or altered stdin is status 6 with nothing created; a held
  lock is status 7.
- `ensure_directory` with `owner`, under the sudo lane: an existing directory with the wrong owner
  and mode is converged; a link at the directory or a parent is status 4 and its target is
  untouched; without `owner` an existing directory is left as it is.

**Unit, every CI host:**

- `update` with `data == observed` sends nothing and returns `False` (I47); with a file changed
  after the caller's read, it raises `FileConflict` and the file keeps the other writer's bytes,
  with no retry (I37, over `local_files`).
- An agent's access refuses an elevated `write` or `ensure_directory`.
- `describe_failure` removes a registered secret that spans two lines of a `CommandFailed`'s stderr
  before taking the tail, neutralizes a control character, and shows no tail for a request sent with
  `log_streams=False`.
- `normalize_destination` accepts `~/x` and `/x/y`, strips a trailing `/`, and refuses `~`, `~/`,
  `x`, `~/../x` and `/a/./b`; `expand_destination` refuses an absolute home or ancestor.

**Callers.** The initializer shards (`cli/tests/test_initializer_*.py`, `test_authorized_keys.py`,
`test_skel.py`, `test_tailscale_dns.py`, `test_git_config.py`, `test_sources.py`,
`cli/tests/agents/`) build MagicMock transports and assert command strings
(`cli/tests/_initializer_support.py:1-9`). They move to two fakes from slice 5
(`cli/tests/vm_access_support.py`): `RecordingVMAccess` for order, identity, elevation, deadline and
failure handling, keyed by each request's named builder rather than a fragment of its text; and
`local_files` for what a step leaves on disk (the zprofile block in each marker case, the source
lines, the fstab edit, sudoers staging). Assertions on a request's exact text go; assertions on
outcomes stay. The reconcile tests run the script with `bash -c` on a temporary home and pass the
archive on stdin (`cli/tests/test_git_credential_scoping.py:65-75`, `:487-560`); they pass the gzip
bytes instead of base64 text, and a recording test checks the request is `Shell.BASH` with the
archive as `Bytes` and both streams discarded. The native settings tests
(`cli/tests/test_native_harness_setup.py`) stay as they are and now run through `update`.

**Conformance vectors.** None new: vectors F1 (stdin out of logs), F2 (exact bytes), F6 (the lock
under `protected_regular`) and F7 (elevated file operations) cover what this slice relies on, and
run on every platform's V lane in the live cells below.

## Incident audit

The entry points this slice moves are `vms/initializer/` (Phase B), `vms/hardening.py`,
`vms/tailscale_dns.py`, `sources.py`, `git_config.py`, `git_credentials/`, `agents/initializer.py`,
`agents/grants.py`, `workspaces/acls.py` and the tmux socket helpers. The audit read their fix
history (`git log --follow` on each, which reaches the monolithic `vms/initializer.py` they were
split from), ADRs 0004, 0009, 0010, 0011, 0012, 0014 and 0017, and the GitHub issues they link (117,
121, 228, 231, 247, 252 and 254). Since the baseline `eee56117c`, the commits on these files that
touch Phase B or the agent paths are `d9c7ada83`, `f2fa3adee`, `bc34330c4`, `1500b436a`,
`4a64820d9`, `78011cce9`, `174a6cab2` and `e099af246`, each covered below; the rest touch Phase A
and the platforms, which slices 3 and 4 re-proved.

**Existing rows this slice touches:**

- **I9** (host keys preserved across stop and start): Phase B's cloud-init drop-in is its reinit
  half (`ssh_keys.py:139-175`); re-proved here by the drop-in's recording test and the live reinit
  cells, beside slice 1's evidence.
- **I19** (the agent probe tells an unreachable VM from an agent that predates the rollout): this
  slice's half is that `agent reinit`, over the new transport, installs the `authorized_keys` the
  probe's hint sends the operator to; re-proved live by reinitializing an agent whose
  `authorized_keys` was removed and then running `agent exec` as it.
- **I25** (the availability hold spans Phase A and Phase B): unchanged,
  `vms/manager/lifecycle.py:619` enters it before both; re-proved by the WSL2 create cell.
- **I28** (a log's redaction set is complete before its first write): the reinit and agent-create
  loggers are built with the credentials and setup values (`reinit.py:257-268`,
  `agents/realize.py:117-124`); unchanged, and F1 covers the reconcile's stdin.
- **I37** (a conditional publication refuses a concurrent edit) and **I47** (a settings write that
  changes nothing leaves the file alone): native settings now publish through `update`, so both are
  re-proved over it, by the `update` unit tests above and the native settings tests.

**New rows, I58 to I79**, in the HLA's inventory, all re-proved in slice 6:

| Row | Behavior                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| I58 | A Phase B step that fails warns, marks initialization partial and lets the next step run; only the required system packages and the #117 DNS latch stop it, and a missing or mismatched host key or a VM that cannot run requests is never absorbed as a step warning (ADR 0004; `72411e418`; `vms/initializer/driver.py:307-653`, `packages.py:180-184`, `tailscale_dns.py:264-270`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| I59 | Initialization order is load-bearing: skel seeds and the zprofile identity block after apt's `--force-confnew` install, the agent's profile fragment before its install commands, the source lines after dotfiles, and `authorized_keys` after every integration's setup (`64a3ce16d`, `422fe3c9d`, `d9c7ada83`; `driver.py:222-223`, `:428-446`, `:645-653`; `agents/initializer.py:233-262`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| I60 | The zprofile identity block is replaced between its markers only when both are present; one unmatched marker warns and the block is appended, leaving the rest of the file alone (`shell_env.py:336-386`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| I61 | A steady-state reinit leaves the files it reconciles alone: the sysctl baseline, the tailscaled drop-in, apt source lists and the sshd fragment are written only when their content differs, and loading the baseline (`sysctl -p`), `daemon-reload`, the sources' `apt-get update` and sshd's validation and reload run only after a write. The sysctl baseline, the tailscaled drop-in and the sshd fragment count as applied only once their follow-up succeeds, and otherwise get their prior content back so the next reinit retries; a source list whose refresh failed is refreshed by the system-packages `apt-get update`, which runs on every reinit with the install, the mise apt key download and the `mise.list` rewrite (ADR 0012; `hardening.py:176-203`; `tailscale_dns.py:97-123`; `packages.py:93-135`; `shell_env.py:398-445`; `steps.update_and_apply`) |
| I62 | The `/proc` hidepid edit changes the existing fstab line in place, keeps an administrator's stricter `hidepid`, leaves a line it cannot parse unchanged with a warning, and then remounts `/proc` with the effective value (ADR 0012; `hardening.py:58-155`, `:206-258`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| I63 | A sudoers fragment reaches its real path only after `visudo -cf` accepts it at a staging name sudo ignores, and the staging file is removed on every path; the sshd fragment is validated with `sshd -t`, the prior state restored on rejection, and sshd reloaded only on success (`9b37c6878`; `shell_env.py:393-504`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| I64 | `authorized_keys` is overwritten with exactly the configured keys; an admin write that ends in any step failure leaves the SSH identity unproven and clears its recorded evidence, while a failed agent write raises, since agent SSH depends on it (ADR 0011; `68961b788`, `4bc0c7978`, `f2fa3adee`, `bc34330c4`; `ssh_keys.py:43-136`; `driver.py:223-288`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| I65 | Admin and agent homes are 0750 over a private primary group (`useradd -U`), and a shared primary group warns with the fix (#228 `dd982461b`, `993ee3ad9`; #231 `6b92e1420`; `agents/initializer.py:142-197`; `shell_env.py:156-205`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| I66 | Agent create-or-converge reports what it found (created, already present, shell corrected) from one `getent` probe, and a fresh create refuses an unowned account or home, a dangling link included (#252 `512ff8589`; `6ae9dd3ff`; `agents/initializer.py:34-68`, `:151-165`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| I67 | Workspace ACLs are applied recursively before the parents' traversal bit is granted again, deny `other`, never add execute to a file, and skip private clone attempts (#247, #254 `93ee11351`, `d5f148937`, `1500b436a`, `78011cce9`; `workspaces/acls.py:20-51`; `workspaces_dir.py:24-73`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| I68 | A failed dotfiles sync skips the install and says the destination keeps its previous content; an install failure is reported on its own (`b5d8efa97`; `driver.py:562-594`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| I69 | An install command is skipped only when every declared completion test passes, never on an empty set, and a test that cannot run counts as not installed (`ed6292920`; `packages.py:219-240`, `:281-292`; `agents/initializer.py:477-487`, `:752-784`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| I70 | The agent's profile fragment is rewritten every time, so reinit clears stale `PATH` entries, with duplicates removed keeping lookup precedence (`e099af246`; `agents/initializer.py:502-521`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| I71 | The global `safe.directory` wildcard is reconciled to exactly one value (`c56a865fe`; `git_config.py:12-14`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| I72 | Git credential reconciliation is one locked script that disables every Agentworks registration before changing material, so a failure leaves authentication absent rather than an older credential reachable; the material travels only on stdin, the script's output is discarded, and failures are reported by exit status with value-free messages (`59c105fb6`, `dbb7e0ac9`, `6c4c334a0`; `git_credentials/reconcile.py:119-325`)                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| I73 | The tailscaled ordering drop-in never restarts tailscaled mid-initialization, and the DNS check runs before any step that needs DNS: the #117 latch raises with the platform-shell heal, any other failure warns (#117 `905d00a73`, `f3588986c`, `8fb56b438`, `174a6cab2`; `tailscale_dns.py:83-270`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| I74 | Agent delete removes the agent's socket directory before `userdel`, so a failing `userdel` cannot orphan it (`dc1dce8eb`; `agents/initializer.py:392-427`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| I75 | Stale-socket cleanup removes only framed, in-scope paths that are still real sockets, and only when their tmux server is confirmed absent (`859318bc0`, `4a64820d9`; `sessions/tmux.py:337-413`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| I76 | The SVE mask is gated on an Apple Virtualization guest that still advertises SVE or SVE2, never reboots mid-initialization, and tells the operator to restart and reinit (`41fe86a2f`, `59ae963ff`; `ssh_keys.py:178-249`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| I77 | Provisioning is hermetic: install commands, mise and the dotfiles installs receive no injected environment, and identity reaches them only through the profile fragments (`driver.py:402-409`; `packages.py:255-258`; `agents/initializer.py:226-231`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| I78 | After the bootstrap, an agent configures itself as its own account: the admin runs only the account, home mode, socket directories and `authorized_keys` (ADR 0011; `agents/initializer.py:83-106`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| I79 | The agent's `.ssh` and `authorized_keys` are placed through the file program's no-follow walk and descriptors, with the agent as owner and their modes applied before publication (ADR 0011; `ssh_keys.py:117-133`; `execution/file_program.py:100-127`, `:265-306`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |

**How each is re-proved, by test.** I58 by driver tests over `RecordingVMAccess`
(`cli/tests/vms/test_phase_b_driver.py`) that fail a step with each of `CommandFailed`,
`ExecutionIncomplete` and `PathRefused` (it warns and every later request is still made) and with a
failed system package install, the latch, `TrustMissing`, `TrustMismatch` and `VMNotReady` (each is
raised), with `cli/tests/vms/test_applied_state_checkpoint.py` mapping a warning to partial and a
raise to failed; I59 by the same fake's request order; I60, I61 and I62 by the steps over
`local_files`, with I61's "no reload" read from the recorded requests; I63 by recording tests in
which `visudo` and `sshd -t` reject, asserting the real path never receives the fragment and the
prior content is restored; I64 by `test_authorized_keys.py` moved (mode, owner, each `STEP_FAILURES`
type on the admin's write giving the unproven outcome, the agent's failure raising); I65 and I66 by
`cli/tests/agents/test_agent_home_permissions.py` and `test_create_reinit_orchestrated.py` moved;
I67 by `test_initializer_workspaces_dir.py`'s order test; I68 by `tests/vms/test_dotfiles.py` and
`test_agent_home_permissions.py::test_a_failed_git_dotfiles_sync_skips_the_agent_install`; I69 and
I70 by `cli/tests/agents/test_install_commands.py` and `test_initializer_apt.py`; I71 by
`test_git_config.py`; I72 by `test_git_credential_scoping.py` and the recording test above; I73 by
`test_tailscale_dns.py`; I74 by its regression test (`dc1dce8eb`); I75 by
`cli/tests/sessions/test_native_socket_cleanup.py` over `access_run_command`, whose `runuser` shim
records that each probe runs as the socket's owner; I76 by the SVE shard; I77 by a recording test
that no Phase B or agent-initialization request carries an environment but apt's `DEBIAN_FRONTEND`
and the tree upload's paths; I78 by a recording test that every agent-phase request uses the agent's
access and none is elevated; I79 by the `ensure_directory` and `write` link tests under the sudo
lane.

**Which rows a live cell exercises** is in Live evidence. I58, I60, I63, I66, I68, I70, I75, I77 and
I78 depend on failures or orderings a live run cannot provoke safely, or that the recording tests
already observe whole, so they have tests and no live cell.

`2841cb9fa` (python3 guaranteed for native helpers) needs no row, as slice 5 found
(`files-lld.md:733-734`). The release-map resolution before any guest mutation (`13f1c6049`,
`4dcd8ea6c`; `driver.py:322-330`) is workstation code this slice does not change.

## Collateral

- **`cli/agentworks/execution/README.md`:** the file-operation table gains `write`, `update` and
  `ensure_directory`'s owner (`:146-163`).
- **`docs/guides/upgrading-to-0.20.md`:** `:137-138` drops "VM and agent initialization" from the
  commands still on the old path; `:237-240` drops "Commands still on the old path log as before"
  for initialization; a section on initialization lists the behavior changes above.
- **`docs/guides/mise.md`:** the sequencing notes say the config and lockfile are never written
  through a link.
- **READMEs:** the git-credential README's reconciler (bash, the archive on stdin, the user's
  access) and the vm-platform README's initialization stage.
- **`docs/guides/idempotency.md`:** the agent's "PATH additions" row (`:134`) becomes "Overwritten
  from template"; the local dotfiles rows (`:96`, `:147`) say the tree is replaced whole; the sshd
  row (`:76`) says it is rewritten and reloaded only on a change; a note says a linked rc file is
  never written through.
- **ADR 0011** (`docs/adrs/0011-direct-target-user-ssh-access.md:26-28`): its stage-and-install
  description becomes `ensure_directory` and an elevated `write` that set the agent as owner through
  no-follow descriptors before the file is published; an "Amended" paragraph, as slice 5 amended
  ADR 0019.
- **Docstrings that describe the old mechanics:** `vms/initializer/driver.py:1-17` ("over the legacy
  Tailscale SSH transport"), `shell_env.py:1-8`, `ssh_keys.py:61-79`, `hardening.py:1-16`,
  `agents/initializer.py:83-106`, `transports/__init__.py:50-54` (the mid-create use of
  `transport_for_user` goes), `execution/files.py:7-10` and `sessions/tmux.py:338-345`.
- **Template field docs:** `dotfiles_destination`'s docstrings (`vms/admin.py:123`,
  `agents/template.py:123`) state the accepted forms, which the rendered samples carry.
- **Release notes** come from the commits; the batch that refuses linked destinations carries a
  `BREAKING CHANGE:` footer naming the warning and the fix.
- **Completions and sample config:** no command, option or setting changes.

**Contracts.** No plugin-facing contract changes. `Files` gains `write` and `update` and
`ensure_directory` gains keyword arguments with defaults, which a plugin can reach through
`ctx.admin_access().files()` and `SetupInvocation.files`; that is additive, and every kind that
receives them was bumped once for this release already (harness-integration 8, git-credential 4,
secret-backend 3, vm-platform 2; `capabilities/*/kinds.py`). `CommandFailed.streams_logged` is a new
attribute on an error plugins may catch, also additive. Every other change is to core callers. No
version bump and no contract footer.

## Live evidence

The plan's cells: create and reinit on every platform from at least one workstation each, with an
admin template that sets dotfiles (git on one cell, a local directory on another), a mise lockfile,
a system and a user install command, and a git credential; and on each, an agent create and reinit
with the same. Each runs the V vectors and F6. The rows each exercises:

| Check                                                                                                                                                                                                                                                        | Cells                            | Rows            |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------- | --------------- |
| Create and reinit complete; homes are 0750 over private groups; `/proc` is mounted `hidepid=1`                                                                                                                                                               | every platform                   | I62, I65, I67   |
| A second reinit reports complete with no `sysctl -p`, `daemon-reload` or sshd reload, and no `apt-get update` beyond the system-packages refresh, in its log (the mise apt key download and the `mise.list` rewrite still run), and skips installed commands | every platform                   | I61, I69        |
| The cloud-init drop-in is present after reinit                                                                                                                                                                                                               | every platform with cloud-init   | I9              |
| `git config --global --get-all safe.directory` returns one `*`                                                                                                                                                                                               | one cell                         | I71             |
| An agent with an `sh` login shell and a git credential reconciles it and clones over HTTPS                                                                                                                                                                   | one cell                         | I72             |
| `agent exec` after an agent reinit that restored a removed `authorized_keys`                                                                                                                                                                                 | one cell                         | I19, I64        |
| `agent reinit` leaves `.ssh` at 0700 and `authorized_keys` at 0600, both owned by the agent                                                                                                                                                                  | one cell                         | I79             |
| A reinit after replacing `~/.bashrc` with a link into the dotfiles checkout, with and without the source line                                                                                                                                                | one cell                         | I59, decision 3 |
| Agent delete leaves no socket directory                                                                                                                                                                                                                      | one cell                         | I74             |
| The DNS check runs before apt on create                                                                                                                                                                                                                      | every platform                   | I73             |
| The SVE gate is a no-op                                                                                                                                                                                                                                      | Lima from macOS on Apple silicon | I76             |
| Create holds through idle shutdown                                                                                                                                                                                                                           | WSL2                             | I25             |

## Delivery

Two batches, each green on its own, in the order they land in PR C. **The rule: a batch that changes
a shared helper moves every caller of it**, so no helper exists in two forms between batches.

- **A, the system files and the shared helpers.**
  - New: `write` and `update` in the program and `Files`, with their tests and the plan's "mode and
    owner before the rename" test; `CommandFailed.streams_logged`; `vms/initializer/steps.py`.
  - Every VM Initialization step on the admin's access: `ssh_keys.py`'s host-key and SVE steps,
    `hardening.py`, `tailscale_dns.py`, the sshd and sudoers fragments, the identity profile and
    zprofile block, the skel seeds, `packages.py` and snap.
  - `_run_install_commands`, with both its callers, the system and the user install commands
    (`driver.py:469`, `:627`).
  - `apply_workspace_acls` with all its callers: `_setup_workspaces_directory`
    (`workspaces_dir.py:63`) and the four slice 7 ones.
  - The socket helpers with all their callers: Phase B (`driver.py:493-501`), agent create
    (`agents/initializer.py:205-207`, passing an admin access built beside its target) and
    `create_session` (`sessions/tmux.py:887-891`, with its callers `_create_roll.py:426` and
    `_lifecycle.py:1066`); `access_run_command`; `cleanup_stale_admin_sockets` deleted.
  - `apply_vm_setup` on Phase B's `files`; `bootstrap_vm` and reinit passing the access; native
    settings on `update`.
  - **Bridges A leaves**, each gone in B: the driver and reinit still build the legacy target
    (`legacy_target`), which the Admin Initialization steps other than the workspaces directory and
    the user install commands, and the final `authorized_keys` write, take beside the access; agent
    create's socket calls use an admin access beside its legacy targets. In the tests,
    `route_vm_access_to_target` and `files_over_target` (`cli/tests/vm_access_support.py`) run a
    moved step's requests on a legacy transport double, and `tmux.CommandOutcome` mirrors
    `SSHResult` for the `RunCommand` readers; slice 7 removes them with the last legacy targets.
- **B, the user files, agents and closure.**
  - New: `ensure_directory`'s owner and elevation, with their tests; `upload_tree`, with the plan's
    tree-upload test; `ensure_source_line` and `source_files`; `normalize_destination` and
    `expand_destination` on both templates.
  - The Admin Initialization steps; `_harden_admin_home` and the login shell; `mise.py`.
  - The shared user-scope helpers, each with both callers: `_reconcile_authorized_keys` (the
    admin's, `driver.py:223`, and `owner=`, `agents/initializer.py:214-220`);
    `ensure_safe_directory_wildcard` (`driver.py:545`, `agents/initializer.py:276`);
    `configure_user_git_credentials` (`driver.py:553`, `agents/initializer.py:285`); `fetch_dir`
    (`driver.py:575`, `agents/initializer.py:360`); `fetch_file` (`mise.py:94`,
    `agents/initializer.py:701`); `_ensure_agentworks_files_sourced` (`driver.py:648`,
    `agents/initializer.py:384`; as built, the agent's only).
  - The rest of `agents/initializer.py`, with the shims fold, and `agents/grants.py`;
    `git_credentials/` with the archive as bytes.
  - The legacy target gone from `bootstrap_vm`, reinit and agent create; every migrated file on the
    static check; the inventory rows; ADR 0011, the guides and the docstrings.

Each batch carries the collateral it makes true.

**Batch B as built.** The bridges batch A left are gone from production code: `bootstrap_vm`, reinit
and agent create build no legacy target, and no initialization step takes one. The test bridges
`route_vm_access_to_target` and `files_over_target` stay for slice 7's callers; agent create's tests
no longer use them. The departures from the tables above, each recorded where it applies:

- The agent shares Phase B's profile, rc, install-command and mise helpers (above, "Shared with the
  admin"), so its install-command completion test is `-lic` and those helpers' warnings reach its
  operation log.
- `ensure_directory` converges with `owner` or `group`, either one.
- `UPLOAD_TREE_SCRIPT` moves an `old` sibling back only when it is a directory and not a link, and
  removes anything else there; it makes each sibling writable and searchable (never through a link)
  before removing it, and every removal is best effort, so a tree with read-only directories is
  replaced on every upload and `restore` always reaches its rename. The git file source requires
  `mktemp`'s output to be a guest path under its staging prefix before using it, and reads its
  staging removal's completion and status, warning when it fails.
- The agent follows the admin's order: its mise config (when `mise_packages` is set), dotfiles, mise
  lockfile and install (when packages, a lockfile or a config in the home, `mise._has_mise_config`),
  then its user install commands, with mise's shims always on its profile's `PATH`
  (`_write_agent_mise_config`, `_install_agent_mise`). Through v0.19.0 the agent ran its install
  commands, then dotfiles, then mise, so its template's config overwrote the dotfiles'.
- One git sequence for every case (above, "One dotfiles sync"): the ref, or `HEAD`, is fetched and
  checked out detached after any clone, and a failed sync skips the install for the agent too.
- A bare relative `dotfiles_destination` is a path in the home, and one that is, holds or lies
  inside a managed path is refused (above, "Dotfiles destinations").
- Both templates validate `dotfiles_source` at load with `sources.check_source_ref`, whose
  `SourceRefFormError` (a `ValueFreeError`) names the accepted forms and no part of the value, so
  doctor and `resource show` never meet an unparsable stored source.
- A `git::https://` URL carrying a user name or token is refused without being echoed, `--` precedes
  the URL in `git clone` and `git remote add`, and the subpath's component check is the parser's. A
  local path holding `::` or `://`, or a colon in its first `/`-delimited component other than a
  Windows drive letter (git's scp-style rule), is refused, so a remote written without `git::` is
  never read as a local file. An existing clone's origin is read with
  `git config --get remote.origin.url`, as configured, since `remote get-url` applies `insteadOf`.
- A directory at a write's fixed temporary name is `PathRefused(UNSUITABLE)` and left alone.
- The admin's final `_ensure_agentworks_files_sourced` pass is gone: its fragment writers run after
  the dotfiles install and every install command and ensure their own lines. Agent setup keeps it.
- The admin's `authorized_keys` warning carries the error's message only, since `write` logs its
  streams.
- `remove_from_workspace_group` and agent delete's first two requests read no status but require a
  completion, so an unreachable VM still raises, as `SSHError` did.
- Native setup's apply functions type their log as `harness_setup.lifecycle.SetupLog`, and the
  driver and agent initialization as `steps.InitLog` (above), so none of them names `SSHLogger`.
- Three tests that asserted on the source text of `_phase_b_setup` and `create_agent_on_vm`
  (`cli/tests/test_secrets_eager_resolve_vm_agent.py`) gave way to recording tests of the same order
  and hermeticity (I59, I77, I78).

## Decisions

The lead accepted each recommendation on 2026-11-09, and the design above follows them; decision 3
went to the operator.

1. **Scope.** Move `workspaces/acls.py`, the tmux socket helpers and stale cleanup (with
   `access_run_command` and `create_session(admin_files=)`) in this slice, because Phase B and agent
   create call them.
2. **Native settings on `update`.** `plugins/_harness_native/native.py:139-183` reads, computes on
   the workstation, skips an unchanged result and calls `write_if`, which is `update` with the read
   as `observed`. It moves in batch A and keeps its own `FileConflict` hint (`native.py:177-183`).
3. **Linked rc files.** The operator ruled to refuse and warn. The design above follows the HLA's
   rule: never write through a link, and accept silently a linked rc file whose target already holds
   the source line.
   - **The consequence.** For an account whose rc files are links into a dotfiles checkout that does
     not source the Agentworks fragments, the fragments stop being sourced: `AGENTWORKS_AGENT`, the
     identity exports and the install commands' and mise's `PATH` entries vanish from that account's
     login shells, and every reinit stays partial with the warning until the operator adds the lines
     to the dotfiles. A linked mise config is not written either, so `mise_packages` stops reaching
     it, with the same warning and status. Today's append through the link keeps all of that working
     but edits the checkout, which then blocks the next `git pull`
     (`agents/initializer.py:345-346`).
   - **The alternative**: write through a link whose resolved target is a regular file owned by the
     same unelevated user, inside that user's home. The file program would gain a second walk mode
     that resolves links one component at a time, refuses any step that leaves the home, and checks
     the final target's owner and type before the conditional write on the resolved path, with tests
     for each escape. That is about 40 lines and a new rule beside "never follow a link" for every
     reader of the program, kept to unelevated writes in the identity's home. It keeps today's
     working shells for linked dotfiles, and keeps today's cost too: the checkout is edited, so the
     next pull still blocks.
   - **Ruling:** refuse and warn ([FRD, 2026-11-09](./frd.md#2026-11-09)). The upgrade guide states
     the consequence and the line to add.
4. **Local dotfiles warning.** The replacement is an output line, not a warning.
5. **Dead code.** `cleanup_stale_admin_sockets` is deleted, not ported.
6. **The reconcile archive** travels as bytes, without base64.
