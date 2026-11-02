# Execution tests

Run everything local from `cli/`:

```console
uv run pytest tests/execution
```

The package's tests pass with the six legacy modules blocked: `conftest.py` here evicts them from
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
- `test_oplog.py`: the operation log record holds a request's shape and never its secrets.
- `test_root_script.py`: the root-script wrapper over fabricated channel output (the status rule,
  the secret in no field, one dispatch), `read_host_keys`, and `run_process` against a local Bash.
- `legacy_modules.py`: the six legacy module names, shared by the conftest blocker, the fresh
  interpreter guard and the static import check.

## Conformance vectors

`conformance/vectors.py` is one list of vectors, each naming the lanes that run it, and
`conformance/lanes.py` has the three lanes:

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
