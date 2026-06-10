# Direct target-user SSH access: plan

**Status:** Draft **Repo:** `agentworks`

The plan is phased. Each phase ends at a green CI and a usable intermediate state. The hidepid
verification is phase 1 because everything downstream assumes its result.

## Phase 1: Empirical hidepid=1 verification

Goal: confirm the kernel-semantics assumption from FRD R5 holds on the actual systems we provision
before any other work commits to it.

- [ ] Provision a lima VM with default agentworks settings.
- [ ] Remount `/proc` with `hidepid=1`: `sudo mount -o remount,hidepid=1 /proc`.
- [ ] Create or identify a process owned by a non-admin Linux user on the VM. Simplest path:
      `agent create` and use its tmux server's pid. Alternatively, `sudo -u nobody sleep 300 &` as a
      one-off.
- [ ] As the admin user (non-root), run `test -d /proc/<other-pid>; echo $?`. Expected: `0`.
- [ ] As the admin user (non-root), run `cat /proc/<other-pid>/cmdline; echo $?`. Expected: non-zero
      exit (permission denied).
- [ ] As root via sudo, both calls succeed. Confirms the sudo fallback would work if needed.
- [ ] Repeat the test on each platform we provision (lima, azure, wsl2, proxmox). WSL2 in particular
      runs a Microsoft-patched kernel and is the most likely platform to deviate from vanilla procfs
      semantics.
- [ ] Document the result in this plan file (check the box and add a one-line note with the kernel
      version tested per platform).
- [ ] **Decision point** (per-platform):
  - All platforms pass: proceed with the plan as written.
  - Specific platforms deviate: route those platforms' pid-check call sites through sudo while
    leaving the other platforms direct. The pid-check call sites are confined enough
    (`sessions/manager.py:_pid_alive`, `sessions/manager.py` batch status compound,
    `sessions/tmux.py force_kill_tmux_server` two checks) that a per-platform branch on the sudo
    decision is straightforward.
  - Every platform fails: route all four call sites through sudo unconditionally.

Definition of done: the result is documented here per platform, and the rest of the plan proceeds
with a known sudo / no-sudo answer per platform.

## Phase 2: VM hardening

Goal: bake the hardening into VM provisioning. Independent of any SSH/access changes; lands the
security baseline so subsequent phases can rely on it.

- [ ] `vms/initializer.py`: write `/etc/sysctl.d/99-agentworks.conf` with the sysctl set from HLA.
      Content-compare against the existing file; only write and run `sysctl --system` when content
      changed.
- [ ] `vms/initializer.py`: add the `hidepid=1` `/proc` mount option to `/etc/fstab` using the
      `# agentworks: hidepid` sentinel from HLA. On reapply: if the sentinel line matches exactly,
      no-op; if the mount-options differ, rewrite the line in place. Never duplicate.
- [ ] Live remount: `mount -o remount,hidepid=1 /proc` (naturally idempotent).
- [ ] Apply at both `vm create` and `vm reinit`. Same code path; declarative.
- [ ] Unit tests: fresh fstab gets the sentinel line; existing fstab with the sentinel and matching
      options is a no-op; existing fstab with the sentinel and differing options is rewritten in
      place. Same shape of test for the sysctl file.
- [ ] Integration smoke: `vm reinit` on an existing VM applies the hardening without breaking
      running session / console state, and re-running it produces no observable side effects.

Definition of done: a freshly-provisioned VM has `hidepid=1` and the sysctl set in place; a reinit
of a pre-existing VM converges to the same state and is silent on the second run.

## Phase 3: Agent lifecycle updates (authorized keys + operator SSH config)

Goal: extend agent create / reinit / delete to manage the on-VM agent `authorized_keys` file and the
operator-side SSH config block. One declarative path that satisfies first-time create and subsequent
reinit identically.

- [ ] `vms/initializer.py`: extend `_reconcile_authorized_keys` with an optional `owner=` parameter.
      When set, switch to the stage-and-install path:
      `sudo install -d -o <owner> -g <owner> -m 0700 <home>/.ssh` to ensure the directory exists;
      `scp` to `/tmp/agw-ak.XXXXXX`;
      `sudo install -o <owner> -g <owner> -m 0600 <staging> <home>/.ssh/authorized_keys`;
      `sudo rm -f <staging>`. When unset, preserve today's direct-write behavior for admin.
- [ ] `agents/manager._create_agent_on_vm`: after the agent's Linux user exists, invoke
      `_reconcile_authorized_keys` with the agent's home as `home=` and `owner=agent.linux_user` to
      trigger the staging path.
- [ ] `agents/manager.reinit_agent`: identical invocation. No special-casing for first-time vs
      subsequent runs.
- [ ] `agentworks/ssh_config.py:_rebuild_config_dir`: extend the `for vm in db.list_vms()` loop to
      also emit one per-agent `Host` block per VM (alias suffix `--<agent.linux_user>` appended to
      the existing VM alias; `User` overridden to the agent's Linux user; all other fields inherited
      from the per-VM admin block). No new add / remove code path; the declarative rebuild handles
      state transitions.
- [ ] `agents/manager._create_agent_on_vm`: call `sync_ssh_config(config, db)` after the agent row
      is written, so the per-agent block appears in the operator's SSH config.
- [ ] `agents/manager.reinit_agent`: same call (declarative rebuild picks up any changes).
- [ ] `agents/manager.delete_agent`: same call after the DB row removal so the rebuild drops the
      block.
- [ ] Verify by direct SSH: after `agent create`, `ssh <prefix><vm>--<agent>` succeeds with the
      operator's key and lands the operator in the agent's shell.
- [ ] Tests: fresh agent has expected on-VM `authorized_keys` content and a corresponding per-agent
      block in the operator's SSH config; reinit after editing `operator.extra_ssh_public_keys`
      reflects the change; delete drops both the on-VM file (via `userdel --remove`'s home directory
      removal) and the operator-side block.

Definition of done: operators can `ssh <prefix><vm>--<agent>` and land directly in the agent's
shell. This is user-visible value as of phase-3 merge, not just plumbing for phases 5 and 6:
anything that targets SSH aliases (VS Code Remote-SSH, ad-hoc scp, manual ssh) gains agent targeting
at this point.

## Phase 4: Target-user SSH plumbing in code

Goal: introduce `agent_exec_target`, leave the call sites unchanged.

- [ ] `agentworks/ssh.py`: add `agent_exec_target(vm, config, agent) -> ExecTarget` that produces an
      ExecTarget connecting as the agent's Linux user. Internally factor the shared builder under
      both `admin_exec_target` and `agent_exec_target`.
- [ ] `admin_exec_target` unchanged from the caller's view (continues to return ExecTarget).
- [ ] Tests for the new helper: produces the right SSH user, identity file, proxy-jump,
      Tailscale-host derivation; `-t` flag is set in interactive mode and not in non-interactive
      mode; connects successfully to admin and to an agent user on a fresh VM.

Definition of done: the new helper exists, is well-tested, and produces ExecTargets that connect
successfully as admin and as agents. No call sites converted yet.

## Phase 5: Convert agent-session creation to direct SSH

Goal: agent-mode session create / restart uses direct-agent SSH; the `sudo --login -u <agent>`
prefix is removed from `sessions/tmux.py`.

- [ ] `sessions/tmux.py:create_session` (agent branch): drop the `sudo --login -u <agent>` prefix;
      the caller passes an agent-targeted `ExecTarget`.
- [ ] `sessions/manager.py:create_session` / `restart_session`: choose `agent_exec_target(...)` when
      the session is agent-mode, `admin_exec_target(...)` when admin-mode. Thread the appropriate
      ExecTarget through to tmux helpers.
- [ ] Socket chmod (`chmod g+rwx <sock>`) runs as the agent (the socket owner). Drop the `sudo` and
      the `run_as_root` parameter on the relevant helpers.
- [ ] `_grant_server_access` runs as the agent (granting admin access to the agent's tmux server's
      `server-access` ACL). The function stays load-bearing for admin's existing read path into
      agent tmux servers per FRD R1's carve-out.
- [ ] In `_grant_server_access` itself (`sessions/tmux.py:248`), drop the inner `sudo -u <q_user>`
      prefix on the `tmux server-access -a ...` call. The caller is now the agent, so sudo'ing to
      the agent is redundant and confusing. Keep the function's outer shape (caller passes the
      appropriate `ExecTarget`); only the inner sudo goes.
- [ ] Tests: create an agent-mode session; verify the resulting tmux server runs as the agent uid;
      admin can still attach via the existing socket / group access; restart cycles work.
- [ ] Verify against a pre-conversion session (created under the old admin+sudo pattern): attach /
      list / restart still work; restart picks up the new code path; the new tmux server is
      structurally identical (per FRD R6).

Definition of done: all new agent-mode sessions are created via direct-agent SSH. Admin's existing
read / attach / maintenance paths into agent tmux servers continue to work unchanged (FRD R1
carve-out, R6 migration).

## Phase 6: Convert `agent shell` to direct SSH

Goal: `agent shell` SSHs as the agent directly, no sudo step.

- [ ] `agents/manager.shell_agent`: replace both branches (with workspace, without) with a single
      direct-agent SSH path via `agent_exec_target` + `interactive`. `cd <workspace>` if a workspace
      was provided; otherwise an interactive login shell at the agent's home.
- [ ] Tests: `agent shell` without workspace lands at the agent's home; with workspace lands at the
      workspace path.
- [ ] Manual UX check: shell starts cleanly, env contains agent's `$USER`/`$HOME`, no sudo-related
      transient output.

Definition of done: `agent shell` no longer routes through admin+sudo.

## Phase 7: Documentation and ADR

Goal: capture the rationale alongside the work.

- [ ] ADR draft in `docs/sdd/2026-06-06-direct-user-ssh-access/new-adrs/` covering the access model
      decision and the VM hardening choices. Will be numbered and moved to `docs/adrs/` when this
      SDD merges.
- [ ] Update `cli/README.md` if any operator-facing surface changes (`agent shell` UX, the new
      per-agent SSH aliases). The aliases are the most user-visible addition; document the
      `awvm--<vm>--<agent>` shape.
- [ ] Update the env-and-secrets SDD's HLA and CLI-side-secret-injection ADR to reference this SDD's
      three-mode framing and assume direct target-user SSH as the access model. **Merge order**:
      this SDD is the precondition and is expected to merge first; if env-and-secrets somehow lands
      first, defer these cross-SDD edits to a separate PR rather than blocking either SDD.

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
