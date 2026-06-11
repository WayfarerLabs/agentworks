# Direct target-user SSH access: plan

**Status:** Draft **Repo:** `agentworks`

The plan is phased. Each phase ends at a green CI and a usable intermediate state. The hidepid
verification is phase 1 because everything downstream assumes its result.

## Phase 1: Empirical hidepid=1 verification

Goal: confirm the kernel-semantics assumption from FRD R5 holds on the actual systems we provision
before any other work commits to it.

Per-platform verification. On each provisioned VM:

1. Remount `/proc` with `hidepid=1`: `sudo mount -o remount,hidepid=1 /proc`.
2. Identify a process owned by a different Linux user than the one running the check.
3. Run the cross-uid pid-check pair:
   - `test -d /proc/<other-pid>` (expect exit 0, directory still visible).
   - `cat /proc/<other-pid>/cmdline >/dev/null` (expect non-zero, permission denied).
4. Revert: `sudo mount -o remount,hidepid=0 /proc`.

The cross-uid framing is what matters: same-uid reads always succeed regardless of `hidepid`, so
either drop privileges to a non-owner (`sudo -u <agent> bash -c ...`) or run the check as a user who
is genuinely a non-owner of the target pid. Both directions (agent reading admin / admin reading
agent) exercise the same kernel permission code path; one direction is enough per platform.

Platforms to verify:

- [x] **lima** (2026-06-10): Debian 12 bookworm, kernel `6.1.0-49-arm64`. Cross-uid agent to admin:
      `test -d` returned 0, `cat .../cmdline` returned 1. Result: works as expected.
- [ ] **azure**
- [ ] **wsl2**: the Microsoft-patched kernel is the most likely platform to deviate from vanilla
      procfs semantics. Worth verifying explicitly even if other platforms pass.
- [ ] **proxmox**

**Decision point** (per-platform):

- All platforms pass: proceed with the plan as written.
- Specific platforms deviate: route those platforms' pid-check call sites through sudo while leaving
  the other platforms direct. The pid-check call sites are confined enough
  (`sessions/manager.py:_pid_alive`, `sessions/manager.py` batch status compound,
  `sessions/tmux.py force_kill_tmux_server` two checks) that a per-platform branch on the sudo
  decision is straightforward.
- Every platform fails: route all four call sites through sudo unconditionally.

Definition of done: the result is documented here per platform, and the rest of the plan proceeds
with a known sudo / no-sudo answer per platform.

## Phase 2: VM hardening

Goal: bake the hardening into VM provisioning. Independent of any SSH/access changes; lands the
security baseline so subsequent phases can rely on it.

- [x] `vms/initializer.py`: write `/etc/sysctl.d/99-agentworks.conf` with the sysctl set from HLA.
      Content-compare against the existing file; only write and run `sysctl --system` when content
      changed.
- [x] `vms/initializer.py`: ensure the `/proc` line in `/etc/fstab` has `hidepid>=1`. Implementation
      shipped a **semantic parse-and-edit** of the existing `/proc` line rather than the
      sentinel-line approach originally specified in HLA: find the `proc` fstype row, parse its
      options, edit `hidepid` in place if needed, preserve admin-set `hidepid=2` (stricter than
      default), append a `/proc` row only when none exists. Action codes returned by
      `_ensure_proc_hidepid_in_fstab` are: `no-op`, `appended`, `added-option`, `upgraded`,
      `preserved-stricter`, `malformed`. This change is intentional and removes the foot-gun of a
      sentinel that admins can edit out by hand.
- [x] Live remount: `mount -o remount,hidepid=1 /proc` (naturally idempotent).
- [x] Apply at both `vm create` and `vm reinit`. Same code path; declarative.
- [x] Unit tests: the fstab editor's decision tree (no-op / append / add-option / upgrade /
      preserve-stricter / malformed) is covered in `tests/test_initializer.py`; the sysctl content
      compare is covered in the same file.
- [ ] Integration smoke: `vm reinit` on an existing VM applies the hardening without breaking
      running session / console state, and re-running it produces no observable side effects.

Definition of done: a freshly-provisioned VM has `hidepid=1` and the sysctl set in place; a reinit
of a pre-existing VM converges to the same state and is silent on the second run.

## Phase 3: Agent lifecycle updates (authorized keys + operator SSH config)

Goal: extend agent create / reinit / delete to manage the on-VM agent `authorized_keys` file and the
operator-side SSH config block. One declarative path that satisfies first-time create and subsequent
reinit identically.

- [x] `vms/initializer.py`: extend `_reconcile_authorized_keys` with an optional `owner=` parameter.
      When set, switches to the stage-and-install path: `install -d` ensures `<home>/.ssh`
      ownership/mode, `mktemp` produces a randomized staging path, `write_file` lands content with
      `mode=0600` so it's private even before the atomic install,
      `install -o <owner> -g <owner> -m     0600 <staging> <home>/.ssh/authorized_keys`, then
      `rm -f <staging>` in a `try/finally` so a partial failure doesn't leak the staging file.
      Failure on the `owner=` path raises so the caller's rollback fires; the admin direct-write
      path (`owner=None`) keeps its historical warn-only behavior.
- [x] `agents/manager._create_agent_on_vm`: invokes `_reconcile_authorized_keys` after the agent's
      Linux user exists; `agent_target` is constructed right after to short-circuit any downstream
      step that needs direct agent SSH.
- [x] `agents/manager.reinit_agent`: identical invocation.
- [x] `agentworks/ssh_config.py`: per-agent `Host` blocks are emitted alongside the per-VM admin
      blocks via the same declarative rebuild path.
- [x] `agents/manager._create_agent_on_vm`: calls `sync_ssh_config(config, db)` after the agent row
      lands.
- [x] `agents/manager.reinit_agent`: same call.
- [x] `agents/manager.delete_agent`: same call after DB row removal.
- [ ] Verify by direct SSH: after `agent create`, `ssh <agent_prefix><agent>` (default
      `awagent--<agent>`) succeeds with the operator's key and lands the operator in the agent's
      shell. (Manual UX check; deferred to next-VM-roll smoke.)
- [x] Tests: `tests/test_authorized_keys.py` covers both branches of `_reconcile_authorized_keys`
      (admin direct-write and agent stage-and-install), including the failure-cleanup `try/finally`
      and the owner-path-raises contract; `tests/test_ssh_config.py` covers the per-agent `Host`
      block rebuild.

Definition of done: operators can `ssh <agent_prefix><agent>` (default `awagent--<agent>`) and land
directly in the agent's shell. This is user-visible value as of phase-3 merge, not just plumbing for
phases 5 and 6: anything that targets SSH aliases (VS Code Remote-SSH, ad-hoc scp, manual ssh) gains
agent targeting at this point.

## Phase 4: Target-user SSH plumbing in code

Goal: introduce `agent_exec_target`, leave the call sites unchanged.

- [x] `agentworks/ssh.py`: `agent_exec_target(vm, config, agent)` plus a shared
      `exec_target_for_user` builder factored out of `admin_exec_target` / `agent_exec_target`.
      `exec_target_for_user` is also public (no leading underscore) so `_create_agent_on_vm` can
      call it during create, when the agent isn't in the DB yet.
- [x] `admin_exec_target` unchanged from the caller's view (continues to return ExecTarget).
- [x] Tests for the helpers: `tests/test_exec_target.py` covers SSH user, identity file, proxy-jump,
      and Tailscale-host derivation, plus the `-tt` (force-TTY) behavior on Windows operators.

Definition of done: the new helper exists, is well-tested, and produces ExecTargets that connect
successfully as admin and as agents. No call sites converted yet.

## Phase 5: Convert agent-session creation to direct SSH

Goal: agent-mode session create / restart uses direct-agent SSH; the `sudo --login -u <agent>`
prefix is removed from `sessions/tmux.py`.

- [x] `sessions/tmux.py:create_session` (agent branch): `sudo --login -u <agent>` prefix removed;
      caller passes an agent-targeted `ExecTarget`.
- [x] `sessions/manager.py:create_session` / `restart_session` / `stop_session` / `delete_session`:
      choose `agent_exec_target(...)` for agent-mode sessions (via the shared
      `_build_session_target` helper) and `admin_exec_target(...)` for admin-mode. The probe at
      `_assert_agent_ssh_works` runs BEFORE any destructive action (and, for `create_session`,
      before any state mutation; for `delete_session`, before the confirm prompt).
- [x] Socket chmod runs as the agent; `force_kill_tmux_server` gained a `use_sudo` parameter
      defaulting to True (admin path stays unchanged; agent-SSH callers set False).
- [x] `_grant_server_access` runs as the agent; the inner `sudo -u <q_user>` was dropped.
- [x] Tests: `tests/test_session_transport.py` covers transport identity (agent-mode session's
      `run_command` comes from `agent_exec_target`, not admin+sudo) plus probe-ordering
      (`create_session` probes before state mutation; `delete_session` probes before the confirm
      prompt). `tests/test_error_wrapper.py` covers KI rollback of group / DB state on the agent
      path.
- [ ] Verify against a pre-conversion session (created under the old admin+sudo pattern): attach /
      list / restart still work; restart picks up the new code path; the new tmux server is
      structurally identical (per FRD R6). (Manual UX check; deferred to next-VM-roll smoke.)

Definition of done: all new agent-mode sessions are created via direct-agent SSH. Admin's existing
read / attach / maintenance paths into agent tmux servers continue to work unchanged (FRD R1
carve-out, R6 migration).

## Phase 6: Convert `agent shell` to direct SSH

Goal: `agent shell` SSHs as the agent directly, no sudo step.

- [x] `agents/manager.shell_agent`: single direct-agent SSH path via `agent_exec_target` +
      `interactive`. `cd <workspace>` if a workspace was provided; otherwise an interactive login
      shell at the agent's home. Bonus scope landed in this phase: `agents/manager.exec_agent`
      (non-interactive `agw agent exec`) was converted to direct agent SSH at the same time;
      `_create_agent_on_vm` and its install/mise/plugin helpers all switched to `agent_target.run`
      for "do work AS the agent" steps, retiring the `_run_as_agent` shim entirely.
- [x] Tests: `tests/test_session_transport.py::test_exec_agent_uses_direct_agent_ssh` confirms
      `exec_agent` builds its SSH argv from the agent's ExecTarget and never invokes
      `sudo --login -u`. `_assert_agent_ssh_works` is covered for the ok / 255 / non-255 / SSHError
      paths in `tests/test_agents.py`.
- [ ] Manual UX check: `agent shell` and `agent exec` start cleanly, env contains agent's
      `$USER`/`$HOME`, no sudo-related transient output. (Deferred to next-VM-roll smoke.)

Definition of done: `agent shell` no longer routes through admin+sudo.

## Phase 7: Documentation and ADR

Goal: capture the rationale alongside the work.

- [x] ADR drafts in `docs/sdd/2026-06-06-direct-user-ssh-access/new-adrs/` covering the access model
      decision and the VM hardening choices. Two drafts: `direct-target-user-ssh-access.md` and
      `vm-hardening-at-provisioning.md`. Will be numbered and moved to `docs/adrs/` when this SDD
      merges.
- [x] Updated `cli/README.md` agents section: `agent shell` / `agent exec` UX, the new direct-SSH
      alias surface (`awvm--<vm>` and `awagent--<agent>` together), and the `ssh_agent_host_prefix`
      config knob. The aliases are the most user-visible addition.
- [x] Cross-SDD references: kept brief and unidirectional. The FRD motivation and HLA "Interaction
      with other SDDs" section name env-and-secrets as the downstream consumer; no edits reach back
      into the env-and-secrets SDD or the CLI-side-secret-injection ADR. Those documents own their
      own framing of how they build on this access model. (Originally scoped to edit in both
      directions; narrowed during implementation to keep this PR's scope tight.)

Definition of done: an interested reader can follow the SDD chain start-to-finish and understand
both the env-and-secrets and direct-target-user-SSH decisions in context.

## Sequencing notes

- **Phase 1 must complete before phase 2 merges.** Phase 2 enables `hidepid=1` on all
  newly-provisioned and reinit'd VMs. If phase 1 hasn't documented the per-platform pid-check
  verification yet (especially WSL2), a VM created in the gap could end up with `hidepid=1` plus
  broken pid checks on a platform that turned out to need the sudo fallback. Phase 2 can be
  developed in parallel with phase 1 but cannot land on main until phase 1's per-platform answers
  are documented.
- **Phase 3 must precede phases 5 and 6.** No point routing SSH to agent users that can't accept the
  operator's key, and no point telling operators to use the SSH alias before it exists.
- **Phase 4 must precede phases 5 and 6.** The new helper needs to exist before call sites use it.
- **Phases 5 and 6 are independent of each other.** Either order; either separately, or together.
- **Phase 7 can land continuously through the work** (incremental doc updates) and is closed out at
  the end.

A reasonable PR shape: one PR per phase except phase 4 (combine with phase 5 since it is purely
preparatory), with phase 7 woven in as needed.
