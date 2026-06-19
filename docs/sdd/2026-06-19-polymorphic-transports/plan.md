# Polymorphic transports -- implementation plan

## Phase 1: Transport ABC + concrete classes

Build the new package alongside the existing code. No call site changes yet.

- [ ] `cli/agentworks/transports/__init__.py`: stub (will hold `Transport` + factories).
- [ ] `cli/agentworks/transports/base.py`: `Transport` `abc.ABC` with abstract methods (`run`,
      `interactive`, `run_as_root`, `copy_to`, `copy_dir_to`). `SSHResult` stays in its current
      home; import it from `agentworks.ssh` for now.
- [ ] `cli/agentworks/transports/ssh.py`: `SSHTransport`. Lifts the SSH argv builders, `SetEnv`
      handling, sudo-wrap, login-shell wrapping from `cli/agentworks/ssh.py`.
- [ ] `cli/agentworks/transports/lima.py`: `LimaTransport`. Local `limactl shell`.
- [ ] `cli/agentworks/transports/remote_lima.py`: `RemoteLimaTransport`. SSH-to-host plus
      `limactl shell`, with the `$SHELL -lc` wrapping baked in for both interactive and
      non-interactive paths.
- [ ] `cli/agentworks/transports/wsl2.py`: `WSL2Transport`. `wsl.exe` with the configured user.
- [ ] `cli/tests/transports/test_ssh.py` (etc., one per transport): each transport tested in
      isolation against the ABC contract (`run`, `interactive`, `run_as_root`, copy ops). Subprocess
      calls mocked via `unittest.mock.patch`.
- [ ] `cli/tests/transports/test_abc.py`: each concrete transport claims to be a `Transport`
      subclass (`isinstance` check); the ABC enforces all abstract methods are implemented.

**Definition of done**: the package compiles, all new tests pass, ruff/mypy clean. Nothing in
`agentworks/` imports the new package yet.

## Phase 2: Factory functions

Wire the new transport into the provisioner and add the two factories.

- [ ] `cli/agentworks/transports/__init__.py`: define `transport(vm, config) -> Transport`
      (Tailscale SSH path, raises `StateError` on missing tailscale_host).
- [ ] `cli/agentworks/transports/__init__.py`: define
      `provisioner_transport(vm, config,     *, stack) -> Transport`. Azure attach/detach via the
      supplied `ExitStack`, reachability probe with the 6-attempt retry loop, NotImplementedError
      wrapping for Proxmox.
- [ ] `cli/agentworks/vms/base.py`: rename `VMProvisioner.admin_exec_target` to
      `VMProvisioner.provisioner_transport`. Return type changes from `ExecTarget` to `Transport`.
- [ ] `cli/agentworks/vms/provisioners/lima.py`: implement `provisioner_transport` returning
      `LimaTransport` (local) or `RemoteLimaTransport` (remote).
- [ ] `cli/agentworks/vms/provisioners/wsl2.py`: implement `provisioner_transport` returning
      `WSL2Transport`.
- [ ] `cli/agentworks/vms/provisioners/azure.py`: implement `provisioner_transport` returning
      `SSHTransport` (against the existing public IP). The empty-host defensive check (PR #118's
      belt-and-suspenders) lives in the factory function, not in the provisioner.
- [ ] `cli/agentworks/vms/provisioners/proxmox.py`: keep the `NotImplementedError`. The wrapping
      into typed `StateError` with the Proxmox-specific web-console hint stays in the factory
      function (already there per PR #118).
- [ ] `cli/tests/transports/test_factories.py`: tests cover both factories, the no-failover
      invariant (R3), the Azure attach/detach lifecycle (both success and exception paths), the
      Proxmox typed-error hint, the reachability-probe retry loop, the defensive empty-host guard.

**Definition of done**: `transport(vm, config)` and `provisioner_transport(vm, config, stack=...)`
return working `Transport` instances. Provisioner methods return `Transport`. Old
`admin_exec_target` / `ExecTarget` paths still exist; nothing yet calls the new factories outside
tests.

## Phase 3: Migrate call sites

Sweep across the ~237 call sites in 23 files. Each replacement: import the right factory, swap the
call, update type hints.

- [ ] `cli/agentworks/vms/manager.py`: every `admin_exec_target` (the
      `agentworks.ssh.admin_exec_target` import) call becomes `transport(...)`. The
      `_provisioner_shell_target` helper becomes a one-liner around `provisioner_transport(...)`, or
      gets inlined.
- [ ] `cli/agentworks/vms/initializer.py`: every `admin_exec_target` and `ExecTarget` use migrates.
      Phase A's `provisioner.provisioner_transport(...)` returns a `Transport` directly.
- [ ] `cli/agentworks/agents/manager.py`: agent_exec_target builder migrates to return a
      `Transport`. Function signatures that accept `ExecTarget` change to `Transport`.
- [ ] `cli/agentworks/workspaces/manager.py`: similar.
- [ ] `cli/agentworks/sessions/manager.py`: similar. The `RunCommand` callback pattern stays
      unchanged (it operates at a different level).
- [ ] `cli/agentworks/sessions/tmux.py`: type hints update to `Transport` where they reference the
      exec target.
- [ ] `cli/agentworks/doctor.py`: similar.
- [ ] `cli/agentworks/remote_exec.py`: similar.
- [ ] Test files (`cli/tests/test_*.py`): stub targets and mocks update to the new shape. Tests that
      assert on `target.ssh`, `target.lima`, etc. update to assert on
      `isinstance(target, SSHTransport)` etc., or are simplified to mock the protocol directly.

**Definition of done**: all 237 references migrated; ruff / mypy / pytest pass; the codebase has no
remaining users of `ExecTarget` or the old `admin_exec_target` function in `agentworks/ssh.py`.

## Phase 4: Delete legacy code

Now that nothing uses the old shape, delete it.

- [ ] `cli/agentworks/ssh.py`: delete `ExecTarget` dataclass.
- [ ] `cli/agentworks/ssh.py`: delete `admin_exec_target` function (replaced by
      `transports.transport`).
- [ ] `cli/agentworks/ssh.py`: delete per-transport helpers (`lima_run`, `_lima_interactive`,
      `wsl2_run`, `_wsl2_interactive`, `remote_lima_run`, `_remote_lima_interactive`,
      `_lima_copy_to`, `_remote_lima_copy_to`, `_wsl2_copy_to`, plus the `LimaTarget` / `WSL2Target`
      / `RemoteLimaTarget` dataclasses).
- [ ] `cli/agentworks/ssh.py`: delete `interactive()` (replaced by `Transport.interactive()`).
- [ ] `cli/agentworks/ssh.py`: delete `_unwrap_ssh()` if no callers remain. (If `sessions/tmux.py`'s
      `RunCommand` still needs it, leave it for a future cleanup as the prior SDD did.)
- [ ] `cli/agentworks/ssh.py`: keep what's genuinely SSH-specific and not Transport-shaped
      (`SSHLogger`, `wait_for_reconnect`, top-level `run` / `run_as_root` callbacks if still used).
- [ ] If `cli/agentworks/ssh.py` is now small enough or has nothing left, fold it into
      `cli/agentworks/transports/ssh.py` or rename to `cli/agentworks/ssh_utils.py`. If it retains
      100+ lines of SSH-specific code, leave it.

**Definition of done**: `git grep "ExecTarget"` returns zero hits (except in commit messages or
historical SDDs); `git grep "admin_exec_target"` returns zero hits; ruff / mypy / pytest clean.

## Phase 5: Tests, docs, PR

Final pass.

- [ ] Run full pytest. Confirm no regressions vs the pre-refactor count.
- [ ] Run `./scripts/lint-files.sh`.
- [ ] Run `ruff` / `mypy` package-wide.
- [ ] `cli/README.md`: no changes expected (operator-visible surface unchanged), but scan for stale
      `ExecTarget` references in code samples.
- [ ] `docs/sdd/2026-06-19-polymorphic-transports/locked.md`: write the lockfile only after the PR
      merges (per the SDD skill, the lockfile lands once the work is complete and the artifacts
      won't change further).
- [ ] Open PR referencing issue #128.

**Definition of done**: PR open, CI green, ready for the agentworks-reviewer pass.

## Risk and mitigations

- **Wide blast radius (Phase 3)**: every consumer of `ExecTarget` touches. Mitigated by keeping
  Phase 1+2 strictly additive (the new code lives alongside the old until Phase 3), so Phase 3 is
  mechanical replacement rather than mixed structural change.
- **Behavioral drift**: easy to introduce a subtle change while moving code between modules.
  Mitigated by Phase 1's isolated tests for each new transport (they assert the same behaviors the
  old per-transport helpers had) and by the existing functional tests for each caller (which
  continue to pass throughout).
- **`sessions/tmux.py`'s `RunCommand` callback uses `SSHTarget` directly**: this is a legitimate
  non-Transport consumer (the prior SDD documented it). Phase 4 keeps `_unwrap_ssh` if it's still
  needed; the cleanup is a separate concern.
- **Long-running migration**: if the work needs to pause partway, the codebase still compiles at
  every phase boundary (old and new coexist until Phase 3 completes). This is the reason for the
  phased approach rather than a single mechanical rename.
