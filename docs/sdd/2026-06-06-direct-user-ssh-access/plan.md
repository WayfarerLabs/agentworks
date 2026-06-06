# Direct user SSH access -- plan

**Status:** Draft **Repo:** `agentworks`

The plan is phased. Each phase ends at a green CI and a usable intermediate state. The hidepid
verification is phase 1 because everything downstream assumes its result.

## Phase 1: Empirical hidepid=1 verification

Goal: confirm the kernel-semantics assumption from FRD R5 holds on the actual systems we provision
before any other work commits to it. If the assumption fails, the rest of the plan shifts to route
pid checks through sudo.

- [ ] Provision a lima VM with default agentworks settings.
- [ ] Remount `/proc` with `hidepid=1`: `sudo mount -o remount,hidepid=1 /proc`.
- [ ] Create or identify a process owned by a non-admin Linux user on the VM (the simplest path is
      to create an agent and use its tmux server's pid; alternatively,
      `sudo -u nobody sleep     300 &` as a one-off).
- [ ] As the admin user (non-root), run `test -d /proc/<other-pid>; echo $?`. Expected: `0`.
- [ ] As the admin user (non-root), run `cat /proc/<other-pid>/cmdline; echo $?`. Expected: non-zero
      exit (permission denied).
- [ ] As root via sudo, both calls succeed. Expected: confirms the sudo fallback would work if we
      needed it.
- [ ] Repeat the test on each platform we provision (lima, azure, wsl2, proxmox). Differences in
      kernel version or procfs patches could matter.
- [ ] Document the result in this plan file (check the box and add a one-line note with the kernel
      version tested).
- [ ] **Decision point**: if `test -d` works cross-uid under `hidepid=1`, proceed with the plan as
      written. If not, add a sub-phase to convert the four call sites to use sudo, and proceed.

Definition of done: the result is documented here and the rest of the plan proceeds with a known
answer about whether sudo is needed.

## Phase 2: VM hardening

Goal: bake the hardening into VM provisioning. Independent of any SSH/access changes; lands the
security baseline so subsequent phases can rely on it.

- [ ] `vms/initializer.py`: write `/etc/sysctl.d/99-agentworks.conf` with the sysctl set from HLA.
      Apply via `sysctl --system`.
- [ ] `vms/initializer.py`: add the `hidepid=1` `/proc` mount option. Either edit `/etc/fstab`
      idempotently or write a systemd mount unit; remount immediately.
- [ ] Apply both at `vm create` provisioning and at `vm reinit` (idempotent reapply).
- [ ] Unit tests for the new initializer functions (write-or-update fstab line idempotently, sysctl
      file content matches expectation).
- [ ] Integration smoke: `vm reinit` on an existing VM applies the hardening without breaking the
      running session/console state.
- [ ] Update `sample-config.toml` comments if any user-visible config knobs are added (none expected
      in this phase).

Definition of done: a freshly-provisioned VM has `hidepid=1` and the sysctl set in place; a reinit
of a pre-existing VM brings it to the same state.

## Phase 3: Authorized keys for agents

Goal: enable direct SSH as agent users by writing their `authorized_keys` at create/reinit.

- [ ] `agents/manager._create_agent_on_vm`: after the agent's Linux user exists, invoke
      `_reconcile_authorized_keys` with the agent's home directory.
- [ ] `agents/manager.reinit_agent`: same invocation; picks up rotation.
- [ ] `vms/manager.reinit_vm`: in the post-init pass, walk agents on this VM and call
      `_reconcile_authorized_keys` for each. Operator key rotation now reaches agent users via
      `vm reinit`.
- [ ] Verify by direct SSH: after `agent create`, `ssh <agent-user>@<vm-tailscale-host>` succeeds
      with the operator's key.
- [ ] Tests: an agent created on a fresh VM has the expected authorized_keys contents; an agent
      reinit after editing `operator.extra_ssh_public_keys` reflects the change.

Definition of done: any agent on a VM can be SSH'd to directly with the operator's key. No code yet
_uses_ this; it's the foundation for phase 4.

## Phase 4: Direct-user SSH plumbing in code

Goal: introduce `ssh_target_for_subject` / `agent_exec_target`, leave the call sites unchanged.

- [ ] `agentworks/ssh.py`: add `ssh_target_for_subject(vm, config, *, subject, interactive)`.
- [ ] `agentworks/ssh.py`: add `agent_exec_target(vm, config, agent)` as a thin wrapper.
- [ ] `admin_exec_target` becomes a thin wrapper too (no behavior change).
- [ ] Tests for `ssh_target_for_subject` covering subject selection, interactive vs non-interactive,
      and the existing identity-file / proxy-jump / Tailscale-host derivations.

Definition of done: the new helpers exist, are well-tested, and produce SSH targets that connect
successfully to admin and to agent users. No call sites converted yet.

## Phase 5: Convert agent-session creation to direct SSH

Goal: agent-mode session create / restart uses direct-agent SSH; the `sudo --login -u <agent>`
prefix is removed.

- [ ] `sessions/tmux.py:create_session` (agent branch): drop the `sudo --login -u <agent>` prefix;
      expect the caller to pass an agent-targeted `ExecTarget`.
- [ ] `sessions/manager.py:create_session` / `restart_session`: choose `agent_exec_target(...)` when
      the session is agent-mode, `admin_exec_target(...)` when admin-mode. Pass the appropriate
      target through to tmux helpers.
- [ ] Socket chmod (`chmod g+rwx <sock>`) is now run by the agent (the socket owner) -- drop the
      `sudo` and the `run_as_root` parameter on the relevant helpers.
- [ ] `_grant_server_access` similarly runs as the agent directly.
- [ ] Tests: create an agent-mode session; verify the resulting tmux server runs as the agent uid
      (same as before), is reachable by admin via the existing socket/group access, and
      attach/restart cycle works.
- [ ] Verify against an existing pre-conversion session (created under the old admin+sudo pattern):
      attach/list/restart still work; restart picks up the new code path; the new tmux server is
      structurally identical.

Definition of done: all new agent-mode sessions are created via direct-agent SSH. Old sessions
continue to function unchanged; their restart enters the new world.

## Phase 6: Convert `agent shell` to direct SSH

Goal: `agent shell` SSHs as the agent directly, no sudo step.

- [ ] `agents/manager.shell_agent`: replace both branches (with workspace, without) with a single
      direct-agent SSH path. `cd <workspace>` if a workspace was provided; otherwise just an
      interactive login shell.
- [ ] Tests: `agent shell` without workspace lands at the agent's home; with workspace lands at the
      workspace path.
- [ ] Manual UX check: shell starts up cleanly, env contains agent's `$USER`/`$HOME`, no
      sudo-related transient output.

Definition of done: `agent shell` no longer routes through admin+sudo.

## Phase 7: Documentation and ADR

Goal: capture the rationale alongside the work.

- [ ] ADR draft in `docs/sdd/2026-06-06-direct-user-ssh-access/new-adrs/` covering the access model
      decision and the VM hardening choices. Will be numbered and moved to `docs/adrs/` when this
      SDD merges.
- [ ] Update `cli/README.md` if any operator-facing surface changes (`agent shell` UX is the most
      likely candidate; mostly invisible).
- [ ] Update `docs/sdd/2026-06-05-env-and-secrets/hla.md` to reference this SDD's three-mode framing
      and assume direct-user SSH as the access model.
- [ ] Update `docs/sdd/2026-06-05-env-and-secrets/new-adrs/cli-side-secret-injection.md` to reflect
      that the agent-mode sudo wrinkle has been retired and that `hidepid=1` is part of the v1
      mitigation for argv-on-SSH exposure.

Definition of done: an interested reader can follow the SDD chain start-to-finish and understand
both the env-and-secrets and direct-user-SSH decisions in context.

## Sequencing notes

- **Phases 1 and 2 are independent.** Phase 2 doesn't depend on phase 1's _result_ (the hardening
  lands regardless); it depends on phase 1's _completion_ only insofar as we want the pid-check
  answer before relying on it elsewhere. They can land in parallel PRs.
- **Phase 3 must precede phase 5.** No point routing SSH to agent users that can't accept the
  operator's key.
- **Phase 4 must precede phases 5 and 6.** The new helpers need to exist before call sites use them.
- **Phases 5 and 6 are independent of each other.** Either order; either separately, or together.
- **Phase 7 can land continuously through the work** (incremental doc updates) and is closed out at
  the end.

A reasonable PR shape: one PR per phase except phase 4 (combine with phase 5 since it's purely
preparatory), with phase 7 woven in as needed.
