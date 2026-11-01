# Implementation Plan: Transport Take 2

- Status: Design under review; no implementation started
- Date: 2026-10-31
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Source baseline: `eee56117c`
- Delivery: one design PR (HLA and plan), then three implementation PRs, each based on `main`

## Delivery rules

- **Three PRs.** PR A carries slices 1 and 2, PR B slices 3 and 4, and PR C slices 5 to 8, as the
  operator chose on 2026-10-31. Each PR is complete: it ships its new pieces together with every
  consumer its slices name, the collateral those pieces outdate, and live evidence for the cells its
  slices list. No vocabulary item or module lands before the slice that migrates its first consumer
  (R10).
- **Slices are review units.** Each slice inside a PR gets its own private review round as it
  completes, and the assembled PR gets a closing review before it goes ready.
- **LLDs come first.** Each slice that needs an LLD starts by writing it in this directory and
  taking it through the private review loop before code. The slice's first checkbox links it.
- **Reviews and validation.** Every slice runs the private quality loop before its PR goes ready:
  `agentworks-reviewer`, `muntz`, and a generic correctness pass for code. It is live-validated
  through the `integration-testing` skill before merge. Missing live coverage goes to the operator
  for disposition and never counts as passing (R11).
- **Independence.** Each slice adds its migrated files to the static legacy-import check (HLA D14).
- **Collateral.** Permanent docs and ADR amendments ride in the slice that makes them true.
- **Merge.** The operator owns merge.

## Design

- [x] Scout the consumer inventory, platform surfaces, SSH mechanics and predecessor proof code at
      `eee56117c`.
- [x] Write the HLA and this plan.
- [x] Run the private project review and complexity pass on the design, and resolve material
      findings.
- [ ] Publish the design PR and address published feedback.
- [x] Record the operator's answers to the HLA's open questions.
- [ ] Record the operator's rulings on the HLA's "Awaiting the operator" items, and amend R3 if the
      operator accepts the proposed wording.
- [ ] Revise the design for the operator's 2026-10-31 decisions (stdin delivery, agw-launch in
      Python, python3 at provisioning, the terminal handover, three PRs), and run the private review
      on the revision.

Definition of done: the HLA and plan are on `main`, and each HLA open question has a recorded
answer.

## Slice 1: Trust (HLA D9; PR A)

- [ ] Write `trust-lld.md`. It covers:
  - trust modes and which factories select each;
  - store layout and alias derivation;
  - the enrollment reset;
  - import pattern matching for plain and hashed records and markers;
  - the re-establishment hook;
  - removal;
  - managed entry options.
- [ ] Implement `execution/trust.py` and the trust errors in `execution/errors.py`.
- [ ] Add `agw vm import-host-keys (--all | VM...) [--known-hosts PATH]`, using the name the
      operator confirmed.
- [ ] Bind the legacy VM-target factories to the store in VM mode: `transport()`,
      `agent_transport()`, `transport_for_user()`, the backup scp, the workspace-copy ssh, the port
      forward and the clouds' public-address native transports. Host targets stay unchanged.
- [ ] Make create reset the store file and run Phase A's first connection in Enroll mode. Make
      delete remove the store file after the platform delete succeeds.
- [ ] Move cloud provisioning SSH to Unverified mode.
- [ ] Have `ssh_config.py` write the store options for VMs that have a store record.
- [ ] Tests:
  - import over plain, hashed, patterned and marked records;
  - the source file is byte-identical after import;
  - a missing record raises `TrustMissing` and never falls back to first use;
  - Enroll mode is reachable only from create;
  - managed entries carry store options only once a record exists.
- [ ] Re-prove incident row I9 (host keys preserved across stop and start).
- [ ] Audit the entry points this slice moves for incident-derived behavior (their ADRs, linked
      issues and fix history), and add incident inventory rows before deleting anything.
- [ ] Collateral: update the command reference and completions, add an operator guide section on the
      one-time import, add release notes, and update `cli/agentworks/execution/README.md`.
- [ ] Live evidence, on every platform: create, the operator's own `ssh` to a managed entry, import
      of an existing VM, and an address change after rekey. Cells:
  - Remote Lima from macOS and Linux;
  - local Lima from macOS;
  - WSL2;
  - Proxmox, AWS, Azure and GCP from at least one workstation each, Windows among them.

  Include an encrypted key served by the agent.

Definition of done: every core connection to a VM verifies against the store, the only acceptance of
a new key is create's enrollment, and nothing in Agentworks writes the operator's `known_hosts` for
a VM connection.

## Slice 2: Execution core and the operator exec surface (HLA D1 to D5, D8, D12 to D14; PR A)

- [ ] Write `execution-lld.md`. It covers:
  - client resolution per workstation;
  - the VM and host policies;
  - the stdin framing and the envelope format;
  - stage 0 and agw-launch's steps, markers and trailer;
  - the terminal handover;
  - request and result types;
  - error mapping;
  - the operation log;
  - the adoption and deletion list for the proof modules.
- [ ] Implement `client.py`, `ssh_process.py`, `envelope.py`, `launcher.py`, `requests.py` (the
      items this slice's consumers use), `results.py`, the remaining `errors.py`, `oplog.py`,
      `access.py`, `attach.py` and `forwarding.py`.
- [ ] Delete `execution/carrier.py`, `execution/preparation.py`, `execution/bootstrap.py`,
      `execution/carriers/` and their tests, after adopting what the LLD lists.
- [ ] Add the conftest blocker under `cli/tests/execution/`, the static legacy-import check, and the
      recording fake `VMAccess`.
- [ ] Migrate `vm exec`, `vm shell`, `agent exec`, `agent shell`, `vm verify-connection`, release
      probing (`vms/manager/release.py`) and `vm port-forward`. Keep `vm exec`'s single-argument
      form as a shell command line, sent as a `Script` under the identity's default shell, and
      several arguments as a literal argv, as today (`vms/manager/exec.py:270`).
- [ ] Type a VM without python3 as `VMNotReady` with a hint naming `agw vm reinit`, whose legacy
      Phase A still runs there, and say so in release notes.
- [ ] Limit the admin and agent template `shell` fields to `sh`, `dash`, `bash` and `zsh` at the
      config boundary (HLA D4), with sample-config comments and release notes.
- [ ] Move the old `LOG_DIR` readers to `oplog`'s constant.
- [ ] Replace doctor's `ssh` and `scp` presence check with the resolver's verdict.
- [ ] Conformance vectors, run against a local sshd and python3 on Linux and on live targets:
  - literal argv, including empty arguments, whitespace and shell characters;
  - script versus stdin, with script source absent from guest argv;
  - every interpreter and startup choice;
  - environment byte-exactness, including `%`, quotes, `${...}`, CR, LF and every other byte but
    NUL;
  - working directory;
  - agent elevation refused before dispatch, and an elevated attach refused;
  - exit statuses 0, 1 and 255;
  - stage 0 under each supported login shell, with startup files that print and export;
  - output before the start marker dropped;
  - not delivered versus unknown, and status 127 typed as `VMNotReady`;
  - exact stdin positioning, so the payload reads the caller's first byte;
  - the terminal handover: the timeout, and cleanup when the second connection fails;
  - a startup file that reads stdin, or redirects a standard stream, which leaves the request not
    delivered;
  - signal dispositions and descriptors reset for the payload, and `TERM` kept in TTY mode;
  - the trailer under a background child;
  - the deadline;
  - sensitive stdin absent from logs and exceptions;
  - binary stdin and stdout on Windows.
- [ ] Re-prove incident rows I1 to I7.
- [ ] Amend ADRs 0013 and 0014.
- [ ] Audit the entry points this slice moves for incident-derived behavior (their ADRs, linked
      issues and fix history), and add incident inventory rows before deleting anything.
- [ ] Collateral: update the command reference and guides for any changed behavior, and
      `cli/agentworks/execution/README.md`.
- [ ] Live evidence:
  - all three workstations against Remote Lima;
  - Linux and macOS against local Lima;
  - Windows against WSL2;
  - one cloud.

  Include a Debian 12 and a Debian 13 guest.

Definition of done: the migrated commands reach VMs only through `agentworks.execution`, their files
pass the static check, and the package's tests pass with the legacy modules blocked.

## Slice 3: First contact and the VM contract (HLA D10, D11 first half; PR B)

- [ ] Write `contract-lld.md`. It covers:
  - the check table and diagnoses;
  - the provisioning bootstrap additions;
  - `tailnet_address`;
  - Phase A on `VMAccess`;
  - `reachability`.
- [ ] Add python3 to `PROVISIONING_PACKAGES`, and confirm that every platform's bootstrap installs
      that list, WSL2's and Lima's included.
- [ ] Make `ProvisionResult.tailnet_address` required on every platform, and delete
      `ProvisionResult.native_transport` and Phase A's native `tailscale ip -4` fallback.
- [ ] Move Phase A onto `VMAccess`: the first connection in Enroll mode, then the base checks
      (including Debian attestation), then `reachability.wait` after `post_tailscale_ready`. Hand
      Phase B a legacy target as today.
- [ ] Implement `reachability.py` and the `observation` request option it polls with.
- [ ] Add elevation, with its conformance vectors, for Phase A, its first consumer, and prove an
      elevated request on a fresh, never-initialized Debian 12 and 13 VM.
- [ ] Add `vms/contract.py`. Run the base checks first at reinit and the initialized checks at the
      end of Phase B.
- [ ] Amend ADR 0025.
- [ ] Tests:
  - a VM failing a base check is not reported ready;
  - each diagnosis names reinit or recreation;
  - a platform that cannot report the address fails create.
- [ ] Audit the entry points this slice moves for incident-derived behavior (their ADRs, linked
      issues and fix history), and add incident inventory rows before deleting anything.
- [ ] Collateral: platform author documentation for `tailnet_address`, and the command reference for
      create and reinit diagnoses.
- [ ] Live evidence: create and reinit on every platform from at least one workstation each.

Definition of done: core's work on a new VM starts over SSH at the reported address, and core's
first operation there is the base contract check.

## Slice 4: Recovery and the platform surface (HLA D11 second half; PR B)

- [ ] Write `platform-lld.md`. It covers:
  - `run_recovery`, `RecoveryResult` and its trailer wrapper;
  - `read_host_keys` and its cloud overrides;
  - `recovery_needs_guest_sshd`;
  - `native_shell`;
  - each platform's recovery channel and private access;
  - `public_ssh.py`;
  - the recovery scripts;
  - `vm recover`;
  - each cloud's provider-side host-key fingerprint source.
- [ ] Implement `run_recovery`, `read_host_keys` and `native_shell` on every platform.
- [ ] Rehome native code as plugin-private modules:
  - Remote Lima's host connection, under the host policy, and its host-side detached create;
  - `plugins/proxmox/guest_agent.py`, replacing `plugins/proxmox/transport.py`;
  - the Lima and WSL2 wrappers.
- [ ] Port `tailscale_join.py` and `wsl2_bootstrap.py` off `ExecTransport`.
- [ ] Implement `capabilities/vm_platform/public_ssh.py` for the clouds.
- [ ] Move rejoin, rekey and delete-time logout onto `run_recovery`. Make the activation gate fail
      with a hint naming `agw vm recover` instead of rejoining on its own, as the operator ruled on
      2026-10-31.
- [ ] Add `agw vm recover <vm>`: host-key re-establishment first when the record is missing, then
      access repair (installing python3 when it is missing), rejoin and SSH verification. Repoint
      `VMNotReady` and `TrustMissing` guidance at it.
- [ ] Delete the activation gate's lazy repair-secret protocol, which only its rejoin used:
      `GateTarget.repair_secret_refs`, `_GateSecrets`, and the call sites in `vms/nodes.py` and
      `sessions/manager/_scope.py`. Update the collateral that names it, including the
      `agentworks-reviewer` persona's sanctioned conditional-repair shape.
- [ ] Send cloud recovery's script on stdin rather than in `sudo -n /bin/sh` arguments (R4).
- [ ] Delete `VMPlatform.native_transport`, the `transports.native_transport` factory and
      `native_shell_unavailable_hint`.
- [ ] Tests:
  - each platform declares sshd dependence;
  - recovery reports status, delivery failure or unknown, and never trusts a channel exit code;
  - logout reports the script's status or unknown, and never checks SSH afterwards;
  - a platform without a native shell refuses with `FeatureUnavailable` before credentials are
    resolved.
- [ ] Audit the entry points this slice moves for incident-derived behavior (their ADRs, linked
      issues and fix history), and add incident inventory rows before deleting anything.
- [ ] Collateral: platform author documentation, the command reference, completions, and an operator
      guide section on recovery.
- [ ] Live evidence:
  - rejoin, rekey, delete and `vm recover` on every platform from at least one workstation each;
  - recovery of a broken sshd on WSL2 and on Proxmox;
  - recovery of a lost store record on one cloud and on Remote Lima.

Definition of done: core reaches a native channel only through `run_recovery` or `native_shell`, and
no platform's private access imports a legacy module.

## Slice 5: Files and capability delivery (HLA D6, D12; PR C)

- [ ] Write `files-lld.md`. It covers:
  - each operation's Python program and its use of stdin and stdout;
  - the `flock` protocol;
  - the symlink policy;
  - mode and ownership;
  - cleanup.
- [ ] Implement `PythonProgram`, `files.py` and the file program, and `RunContext.admin_access()`
      and `agent_access()`.
- [ ] Migrate:
  - artifact publication and session artifacts;
  - native probing;
  - the harness-native settings merge and CLI transcript reads;
  - the codex and claude artifact reads;
  - harness integrations' readiness and start stages;
  - the git-credential providers.
- [ ] Tests:
  - `write` applies mode and owner before the rename;
  - `write_if` and `remove_if` detect a concurrent writer;
  - JSON null survives an update;
  - a malformed destination is refused;
  - a symlinked destination is refused;
  - cleanup touches only what the operation created.
- [ ] Amend ADR 0019.
- [ ] Audit the entry points this slice moves for incident-derived behavior (their ADRs, linked
      issues and fix history), and add incident inventory rows before deleting anything.
- [ ] Collateral: update `capabilities/README.md` and plugin author documentation for the new
      accessors.
- [ ] Live evidence: harness setup and a session start with artifacts, on Remote Lima from macOS and
      on one cloud from Windows.

Definition of done: `native_files.py` has no remaining callers.

## Slice 6: Initialization (PR C)

- [ ] Migrate:
  - `vms/initializer/`, `vms/hardening.py` and `vms/tailscale_dns.py`;
  - `sources.py` (with `upload_tree`), `command_checks.py` and `git_config.py`;
  - `git_credentials/`;
  - the agent initializer and grants;
  - the harness setup runners. The `SetupRunner` environment rules become request environment.
- [ ] Replace hand-written `printf | sudo tee`, `tee -a` and `write_file` plus `chmod` sequences
      with `files.write` and `files.update`.
- [ ] Audit the entry points this slice moves for incident-derived behavior (their ADRs, linked
      issues and fix history), and add incident inventory rows before deleting anything.
- [ ] Collateral: guides or reference pages whose described init behavior changes.
- [ ] Live evidence: create and reinit on every platform from at least one workstation each.

Definition of done: create and reinit run entirely on the new transport.

## Slice 7: Workspaces, sessions and backup (HLA D7; PR C)

- [ ] Implement `detached.py`, and the `FromFile` and `ToFile` input and sink.
- [ ] Migrate:
  - workspace backends, rehome and repair;
  - workspace copy, using `run` over an opaque tar stream;
  - `sessions/`, including tmux, consoles, status probes and pid repair, with session environment in
    the request and console panes taking theirs from the terminal handover, so no `tmux -e` argument
    carries a value (HLA D8);
  - backup's launch, observation, interrupt and download, using `detached` and `ToFile` with
    progress.
- [ ] Tests:
  - a fresh launch never matches an earlier one;
  - observation resumes across a dropped connection;
  - interrupt stops the work;
  - the detached base is never world-writable.
- [ ] Retire the newline refusals for environment values (`manifests/decode.py:254-261` and every
      `LineOrientedSecretUse.ENVIRONMENT` use, in `env/compose.py:73-81` and
      `secrets/orchestration.py:228-232`), now that no path sends environment by `SetEnv`.
- [ ] Delete the console's env carry across sudo, which the handover replaces: the `--preserve-env`
      path and its memo in `sessions/multi_console/`, and the `Defaults:<admin> setenv` sudoers
      grant (`_write_sudoers_console_setenv`).
- [ ] Re-prove incident rows I6a and I8.
- [ ] Amend ADR 0017.
- [ ] Audit the entry points this slice moves for incident-derived behavior (their ADRs, linked
      issues and fix history), and add incident inventory rows before deleting anything.
- [ ] Collateral: guides for sessions, consoles and backup where behavior changes.
- [ ] Live evidence:
  - session and console create, attach, detach and restore on Remote Lima from all three
    workstations and on WSL2;
  - backup with a forced disconnect and with an interrupt, on WSL2 (idle shutdown) and Remote Lima.

Definition of done: no workspace, session or backup module imports a legacy module, and
`remote_exec.py` has no remaining callers.

## Slice 8: Removal (PR C)

- [ ] Audit the legacy modules this slice deletes for incident-derived behavior (their ADRs, linked
      issues and fix history), and add incident inventory rows for anything not yet recorded.
- [ ] Confirm that every incident inventory row has recorded evidence.
- [ ] Delete the six legacy modules with their tests:
  - `agentworks.transports`
  - `agentworks.ssh`
  - `agentworks.remote_exec`
  - `agentworks.harness_setup.runner`
  - `agentworks.native_files`
  - `agentworks.plugins.proxmox.transport`
- [ ] Delete `RunContext.admin_target()` and `agent_target()`, and the `SSHError` special case in
      `cli/_entry.py`.
- [ ] Delete the independence guards that can no longer fail once the legacy modules are gone: the
      fresh-interpreter guard, the conftest blocker and the static legacy-import check.
- [ ] Make the managed SSH entries' store options unconditional, with release notes for operators
      who never imported.
- [ ] Search for remaining users of the `AcceptEnv` fragment. If none remain, stop writing it and
      supersede ADR 0014; otherwise record what still needs it in that ADR.
- [ ] Confirm by search that no core code builds its own `ssh` or `scp` command line outside
      `agentworks.execution` and platforms' private access.
- [ ] Complete the evidence matrix, sending any gap to the operator.
- [ ] Confirm that the permanent docs (the `agentworks.execution` README, platform author guidance
      and the operator guides) describe the end state, then write `locked.md`.

Definition of done: R10's end state holds, every matrix cell has evidence or an operator
disposition, and the SDD is locked.
