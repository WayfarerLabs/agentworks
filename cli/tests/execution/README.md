# Execution tests

Run everything local from `cli/`:

```console
uv run pytest tests/execution
```

The package's tests pass with the legacy modules blocked: `conftest.py` here evicts them from
`sys.modules`, removes the attribute on their parent package and puts a finder that refuses them
first on `sys.meta_path`, for every test. `test_independence.py` proves the same at import time in a
fresh interpreter. Tests that exercise a migrated manager live elsewhere in `tests/` and use the
`vm_access_recorder` fixture (`tests/vm_access_support.py`), a `VMAccess` that records requests and
returns canned outcomes without ssh.

## What each file covers

- `test_envelope.py`: every byte but NUL survives the envelope, each refusal, the kernel size
  limits.
- `test_requests.py`: a word starting with `=` reaches zsh literally, whatever the runner's login
  shell.
- `test_stream_filters.py`: the stream filters under every split and interleaving, the classifier
  row by row, the refusal-to-status table.
- `test_ssh_process.py`: the client argv, and `ssh -G` against the installed client.
- `test_client_resolution.py`: the resolver against temporary executables, including the Windows
  candidate order.
- `test_pump.py`: the pump against synthetic children that speak the marker protocol. Marked
  `windows`, so it runs in Windows CI.
- `test_access.py`, `test_launcher_source.py`, `test_stream_process.py`: passivity and ordering, the
  launcher's structure, and `stream` in a real process whose stdin stays open.
- `test_trust.py`: the host-key store.
- `test_reachability.py`: what `reachability.wait` polls, what it raises on the first attempt, and
  the confirming probe, over the recording fake.
- `test_oplog.py`: the operation log record holds a request's shape and never its secrets.
- `test_root_script.py`: the root-script wrapper over fabricated channel output (the status rule,
  the secret in no field, one dispatch), `read_host_keys`, and `run_process` against a local Bash.
- `test_run_process.py`: `run_process` over Python children (the deadline, the output limit, a
  background child, end of file). Marked `windows`, since WSL2's root channel runs only there.
- `legacy_modules.py`: the legacy module names, shared by the conftest blocker, the fresh
  interpreter guard and the static import check.

## Conformance vectors

`conformance/vectors.py` is one list of vectors, each naming the lanes that run it, and
`conformance/lanes.py` has the three lanes. L and S run agw-launch on this machine as the guest, and
agw-launch is a Linux program (`os.memfd_create`, `/proc`), so both skip on macOS and Windows, CI
included; the root channel's W3 skips on a local lane without `/proc`.

- **L** runs `$SHELL -c <remote command>` locally through the real `VMAccess` and pump, with the
  account's startup files set per vector and `SSH_CLIENT` exported as sshd does. It runs under each
  of `sh`, `dash`, `bash` and `zsh`, and under each interpreter available: the system `python3` and
  a 3.11 (`AGW_CONFORMANCE_PYTHON311`, or `python3.11` on `PATH`). A shell that is not installed
  skips locally and fails when `CI` is set.
- **S** runs the real `ssh` client against a rootless `sshd` on loopback, with a temporary host key
  and `AuthorizedKeysFile`, and the host key written to the host-key store. It needs an `sshd`
  binary (`/usr/sbin/sshd` counts, though that directory is often not on `PATH`) and skips without
  one locally, failing when `CI` is set. The account's own login shell is the real one, so S
  exercises stage 0 under whatever shell the user has.
- **V** is the live lane, against a VM named by `AGW_CONFORMANCE_VM`, reached as the operator's
  configured admin with the operator's real database and host-key store:

  ```console
  AGW_CONFORMANCE_VM=my-vm uv run pytest tests/execution/conformance/test_live_vectors.py -m integration
  ```

  The lane runs on one xdist worker (`xdist_group`): Debian's sshd default, `MaxStartups 10:30:100`,
  starts dropping unauthenticated connections at random past 10, so parallel vectors would fail
  unpredictably.

  It creates and removes only `/tmp/agw-conformance-*` and `/tmp/agw-handover-*` on the guest. Run
  it from each workstation the evidence matrix names; Windows is where binary stdin and early exit
  matter most. The lane is not part of the default run.

The elevation vectors (E1, E2 and E6) run on L and V. On L they need the runner's passwordless
`sudo -n`, skipping without it locally and failing without it when `CI` is set; E4, the
distribution's default sudoers on a fresh VM, is V only.

`conformance/program_vectors.py` holds the vectors for the request vocabulary the files LLD adds,
which join the list in `vectors.py`:

- F1 (L, S and V): a request's `Bytes` input reaches no record of a real `SSHLogger` and no error,
  and `log_streams=False` keeps its streams out of the record.
- F2 and F3 (L, S and V): every input byte reaches a `PythonProgram` from its first byte, its
  arguments arrive exactly, and its source is in no argument.
- F4 (L and V): a `PythonProgram` with `login`, `interactive` and both, under startup files that
  print, export and end with a failing command substitution. On L the account's default shell is the
  lane's own (`LocalLane.use_lane_shell_as_account_shell`), so the four shells are each covered; on
  V it is the admin's.
- F5 (L and V): an interactive `Script` under `sh`, `bash` and the default shell sees its
  interactive startup, returns its own `exit` status and prints none of the prompts its startup
  sets, which a shell reading the script as its input would.
- F9 (L and V): a startup file that closes descriptors 3 to 9 still leaves a login and interactive
  `PythonProgram` and an interactive `Script` their source.
- F8 (L and V): an elevated request keeps its environment; on L it needs `sudo -n`, as the elevation
  vectors do.
- The `run` sinks capture to their limit or discard (L and S).

On L, a 3.11 interpreter that is not the system one becomes the program interpreter as well as the
launcher's, so a `PythonProgram` runs under it too.

`conformance/file_vectors.py` holds the file operations' vectors, which join the same list:

- F6 (V): the VM-wide file lock under the image's `fs.protected_regular`. The admin and root each
  create it in turn, and the other then takes it unelevated or elevated and completes a `write_if`.
  The lane reaches the VM as its admin only, so root stands in for a second account. It removes the
  lock file between rounds.
- F7 (S and V): an elevated `write_if` creates a root-owned file that an elevated `read` returns
  (with `sudo -n` on S), and an agent's elevated `write_if` is refused before anything is sent.

Every test but an integration test sends the file program with its lock in the worker's temporary
directory (the suite conftest's `file_lock_path`), so the L and S lanes and
`run_file_program_locally` never take the host's own `/run/lock`.

`test_file_program.py` runs the guest file program's `main` in its own interpreter, under each
interpreter the L lane uses, on a temporary tree with its lock in a sticky directory of its own:
conflicts, truncated input, links at the destination, a parent and the lock path, the lock's mode,
wait and sharing, metadata preservation, cleanup after a failure or `SIGTERM`, and pruning. Its sudo
lane (a lock root created first, owner preservation, a root-owned write) needs the runner's
passwordless `sudo -n` and follows the elevation vectors' rule. `test_files.py` covers `Files`: the
status-to-error mapping, `update`'s retries and refusals before dispatch over the recording access,
and a round trip, a link refusal and the settings merge through `update` over the L lane.

`conformance/test_attach_terminal.py` and `conformance/test_forward.py` cover what happens around
the terminal half and a forward, on the same lanes.

`conformance/root_vectors.py` holds the root channel's vectors (W1 to W9 in the platform LLD), on
two lanes of their own. L runs `root_script.run_process(["/bin/bash", "-s"], ...)` locally, and
again under `sudo -n` where the runner grants it (`conformance/test_root_vectors.py`). V runs
through the platform `run_as_root` of the VM named by `AGW_CONFORMANCE_VM`, inside the same gated
boundary `vm shell --platform` opens:

```console
AGW_CONFORMANCE_VM=my-vm uv run pytest tests/test_root_channel_live_vectors.py -m integration
```

That runner sits outside this directory because the platform boundary imports legacy modules.

## Writing a vector

A vector asserts observable behavior only: a completion, bytes, a status. Do not assert a message's
wording. Take the lane as the only argument and use `lane.run`, `lane.stream`, `lane.make_dir` and
friends, so it runs unchanged on every lane that lists it. A vector that needs the account's startup
files is `L` only and uses `LocalLane.startup`.
