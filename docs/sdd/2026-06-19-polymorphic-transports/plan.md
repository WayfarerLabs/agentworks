# Polymorphic transports -- implementation plan

## Phase 1: Transport ABC + concrete classes

Build the new package alongside the existing code. No call site changes yet.

- [ ] `cli/agentworks/transports/__init__.py`: stub (will hold `Transport` re-export + factories).
- [ ] `cli/agentworks/transports/base.py`: `Transport` `abc.ABC` with the full operation surface as
      abstract methods: `run` (with `sudo` / `tty` / `check` / `timeout` / `env` kwargs),
      `interactive`, `copy_to`, `copy_from`, `copy_dir_to`, `write_file`, `call_streaming`.
      `run_as_root` is **not** a separate method (per the prior locked SDD, `run(sudo=True)` is the
      unified shape). `SSHResult` stays in its current home; import it from `agentworks.ssh` for
      now.
- [ ] `cli/agentworks/transports/ssh.py`: `SSHTransport`. Lifts the SSH argv builders, `SetEnv`
      handling, sudo-wrap, login-shell wrapping from `cli/agentworks/ssh.py`. Implements every
      abstract method, including `copy_from` (via scp), `write_file` (via mktemp + scp +
      atomic-install or via cat-stdin-redirect), and `call_streaming` (via subprocess with inherited
      stdio).
- [ ] `cli/agentworks/transports/lima.py`: `LimaTransport`. Local `limactl shell`. Implements
      `copy_from` via `limactl copy` (reverse direction), `write_file` via the existing local-file +
      `limactl copy` path, `call_streaming` via subprocess with inherited stdio.
- [ ] `cli/agentworks/transports/remote_lima.py`: `RemoteLimaTransport`. SSH-to-host plus
      `limactl shell`, with the `$SHELL -lc` wrapping baked in for both interactive and
      non-interactive paths. `copy_from` two-hops: `limactl copy` to host temp, scp host to local;
      `write_file` symmetric.
- [ ] `cli/agentworks/transports/wsl2.py`: `WSL2Transport`. `wsl.exe` with the configured user.
      `copy_from` via `wsl ... cat` to stdout; `write_file` via `wsl ... bash -c 'cat > path'` with
      stdin.
- [ ] `cli/tests/transports/test_<ssh|lima|remote_lima|wsl2>.py`: each transport tested in isolation
      against the ABC contract (`run`, `interactive`, `copy_to`, `copy_from`, `copy_dir_to`,
      `write_file`, `call_streaming`). Subprocess calls mocked via `unittest.mock.patch`.
- [ ] `cli/tests/transports/test_abc.py`: each concrete transport claims to be a `Transport`
      subclass (`isinstance` check); the ABC enforces all abstract methods are implemented
      (instantiating a transport without one of the abstract methods should raise `TypeError` at
      construction).

**Definition of done**: the package compiles, all new tests pass, ruff/mypy clean. Nothing in
`agentworks/` outside the new package imports it yet.

## Phase 2: Factory functions and provisioner hook

Wire the new transports into the provisioner ABC and add the three factories plus the
transient-route hook.

**Important**: Phase 2 ships with breakage. Renaming `VMProvisioner.admin_exec_target` to
`VMProvisioner.provisioner_transport` and changing the return type from `ExecTarget` to `Transport`
immediately breaks all existing callers (~10 sites in `vms/manager.py` and `vms/initializer.py`).
Phase 3 is the resolution. The codebase compiles at the END of Phase 2 + Phase 3 (effectively one
logical commit boundary spread across two phases for review-size reasons); it does NOT compile in
between. If the refactor needs to pause partway through, the pause point is the end of Phase 3.

- [ ] `cli/agentworks/transports/__init__.py`: define `transport(vm, config) -> Transport` (admin
      via Tailscale SSH).
- [ ] `cli/agentworks/transports/__init__.py`: define
      `agent_transport(vm, config, agent) -> Transport` (named agent via Tailscale SSH).
- [ ] `cli/agentworks/transports/__init__.py`: define
      `transport_for_user(vm, config, *, user, identity_file=None, logger=None) -> Transport`
      (low-level shared core).
- [ ] `cli/agentworks/transports/__init__.py`: define
      `provisioner_transport(vm, config, *, stack) -> Transport`. Uses
      `stack.enter_context(prov.transient_route(vm))` (no isinstance check). Reachability probe with
      the 6-attempt retry loop. NotImplementedError wrapping for Proxmox with the web-console hint.
- [ ] `cli/agentworks/transports/__init__.py`: move `wait_for_reconnect` here from `ssh.py`; take a
      `Transport` instead of `ExecTarget`.
- [ ] `cli/agentworks/vms/base.py`: rename `VMProvisioner.admin_exec_target` to
      `VMProvisioner.provisioner_transport`. Return type changes to `Transport`.
- [ ] `cli/agentworks/vms/base.py`: add
      `VMProvisioner.transient_route(vm) -> AbstractContextManager[None]` with default
      `nullcontext()`.
- [ ] `cli/agentworks/vms/provisioners/lima.py`: implement `provisioner_transport` returning
      `LimaTransport` (local) or `RemoteLimaTransport` (remote). Default `transient_route` is fine.
- [ ] `cli/agentworks/vms/provisioners/wsl2.py`: implement `provisioner_transport` returning
      `WSL2Transport`. Default `transient_route` is fine.
- [ ] `cli/agentworks/vms/provisioners/azure.py`: implement `provisioner_transport` returning
      `SSHTransport` (against the attached public IP). Override `transient_route` as a
      `@contextlib.contextmanager` that calls `attach_public_ip` on enter and `detach_public_ip` on
      exit. The defensive empty-host check (PR #118) lives in the factory function.
- [ ] `cli/agentworks/vms/provisioners/proxmox.py`: keep the `NotImplementedError`. The
      `transient_route` default no-op is fine (Proxmox doesn't reach the route step because
      `provisioner_transport` raises first).
- [ ] `cli/tests/transports/test_factories.py`: tests cover all three factories, the no-failover
      invariant (R3), the Azure `transient_route` lifecycle (success + exception paths), the Proxmox
      typed-error hint, the reachability-probe retry loop, the defensive empty-host guard.

**Definition of done**: factories and the polymorphic `transient_route` hook in place. The
`agentworks/transports/` package is the canonical source for transport construction. The codebase
does NOT compile cleanly at this boundary because the rename in `vms/base.py` breaks ~10 callers;
Phase 3 resolves them.

## Phase 3: Migrate call sites

Sweep across the ~237 call sites in 23 files. Each replacement: import the right factory, swap the
call, update type hints.

- [ ] `cli/agentworks/vms/manager.py`: every `admin_exec_target` (the
      `agentworks.ssh.admin_exec_target` import) call becomes `transport(...)`. The
      `_provisioner_shell_target` helper becomes a one-liner around `provisioner_transport(...)`, or
      gets inlined. The `isinstance(prov, AzureProvisioner)` block goes away (replaced by the
      polymorphic `transient_route` hook called from inside `provisioner_transport`).
- [ ] `cli/agentworks/vms/initializer.py`: every `admin_exec_target` and `ExecTarget` use migrates.
      Phase A uses `provisioner.provisioner_transport(...)` returning a `Transport` directly.
- [ ] `cli/agentworks/vms/backup.py`: replace `_unwrap_ssh(target)` with `target.copy_from(...)` for
      each scp-from-VM call. Remove the `_unwrap_ssh` import. The scp argv building moves to
      `SSHTransport.copy_from` (Phase 1 already added it).
- [ ] `cli/agentworks/agents/manager.py`: `agent_exec_target` calls migrate to
      `agent_transport(vm, config, agent)`. `exec_target_for_user(vm, config, *, user=...)` at the
      one mid-create site migrates to `transport_for_user(vm, config, user=...)`. Function
      signatures that accept `ExecTarget` change to `Transport`.
- [ ] `cli/agentworks/workspaces/manager.py`: type hints update to `Transport`.
- [ ] `cli/agentworks/sessions/manager.py`: `admin_exec_target` -> `transport`, `agent_exec_target`
      -> `agent_transport`. The `RunCommand` callback pattern in `sessions/tmux.py` is satisfied by
      `partial(target.run, ...)` and is unchanged.
- [ ] `cli/agentworks/sessions/tmux.py`: type hints update to `Transport` where they reference the
      exec target. The `RunCommand` Protocol itself is untouched.
- [ ] `cli/agentworks/doctor.py`: similar.
- [ ] `cli/agentworks/remote_exec.py`: similar.
- [ ] `cli/agentworks/workspaces/backends/vm.py`: similar.
- [ ] `cli/agentworks/vms/hardening.py`: type hints update.
- [ ] `cli/agentworks/vms/tailscale_dns.py`: type hints update.
- [ ] Test files (`cli/tests/test_*.py`): stub targets and mocks update to the new shape. Tests that
      assert on `target.ssh`, `target.lima`, etc. update to assert on
      `isinstance(target, SSHTransport)` etc., or are simplified to mock the ABC directly.

**Definition of done**: all `ExecTarget` / `admin_exec_target` / `_unwrap_ssh` / `agent_exec_target`
/ `exec_target_for_user` references in `cli/agentworks/` migrated; ruff / mypy / pytest pass; the
codebase compiles cleanly. This is the end of the "Phase-2-and-3-together" logical commit boundary.

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
- [ ] `cli/agentworks/ssh.py`: delete `_unwrap_ssh()`. Phase 3 migrated its only caller
      (`vms/backup.py`) to `Transport.copy_from`, so this is now unconditional.
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
- [ ] `docs/sdd/2026-06-19-polymorphic-transports/locked.md`: not part of this PR. The lockfile
      lands as a follow-up commit after merge, per the SDD skill.
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
