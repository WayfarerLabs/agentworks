# Implementation Plan: Transport Take 2

- Status: Slices 1 and 2 implemented in PR A (#863), live evidence in progress; slices 3 to 8 not
  started
- Date: 2026-10-31
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Source baseline: `eee56117c`
- Delivery: one design PR (HLA and plan), then three implementation PRs, each based on `main`

## Delivery rules

- **Three PRs.** PR A carries slices 1 and 2, PR B slices 3 and 4, and PR C slices 5 to 8, as the
  operator chose on 2026-10-31. Each PR is complete: it ships its new pieces together with every
  consumer its slices name, whose existing tests stay green, the collateral those pieces outdate,
  and live evidence for the cells its slices list. No vocabulary item or module lands before the
  slice that migrates its first consumer (R10).
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
- [x] Record the operator's rulings on the HLA's "Awaiting the operator" items, and amend R3 if the
      operator accepts the proposed wording.
- [ ] Record the operator's answers on the auth key's handling in provisioning (amending R1 if the
      operator accepts) and on python3 at init.
- [x] Revise the design for the operator's 2026-10-31 decisions (stdin delivery, agw-launch in
      Python, python3 at provisioning, the terminal handover, three PRs), and run the private review
      on the revision.

Definition of done: the HLA and plan are on `main`, and each HLA open question has a recorded
answer.

## Slice 1: Trust (HLA D9; PR A)

- [x] Write [`trust-lld.md`](./trust-lld.md). It covers:
  - trust modes and which factories select each;
  - store layout and alias derivation;
  - the enrollment reset;
  - import pattern matching for plain and hashed records and markers;
  - the re-establishment hook;
  - removal;
  - managed entry options.
- [x] Implement `execution/trust.py` and the trust errors in `execution/errors.py`.
- [x] Add `agw vm import-host-keys (--all | VM...) [--known-hosts PATH]`, using the name the
      operator confirmed.
- [x] Bind the legacy VM-target factories to the store in VM mode: `transport()`,
      `agent_transport()`, `transport_for_user()`, the backup scp, the workspace-copy ssh, the port
      forward and the clouds' public-address native transports. Host targets stay unchanged.
- [x] Make create reset the store file and run Phase A's first connection in Enroll mode. Make
      delete remove the store file after the platform delete succeeds.
- [x] Move cloud provisioning SSH to Unverified mode.
- [x] Have `ssh_config.py` write the store options for VMs that have a store record.
- [x] Tests:
  - import over plain, hashed, patterned and marked records;
  - the source file is byte-identical after import;
  - a missing record raises `TrustMissing` and never falls back to first use;
  - Enroll mode is reachable only from create;
  - managed entries carry store options only once a record exists.
- [ ] Re-prove incident row I9 (host keys preserved across stop and start).
- [x] Audit the entry points this slice moves for incident-derived behavior (their ADRs, linked
      issues and fix history), and add incident inventory rows before deleting anything.
- [x] Collateral: update the command reference and completions, add an operator guide section on the
      one-time import, add release notes, and update `cli/agentworks/execution/README.md`.
- [ ] Live evidence, on every platform: create, the operator's own `ssh` to a managed entry, import
      of an existing VM, an address change after rekey, a stop and start, and the operator's
      `known_hosts` byte-identical after a create. Cells:
  - Remote Lima from macOS and Linux;
  - local Lima from macOS;
  - WSL2;
  - Proxmox, AWS, Azure and GCP from at least one workstation each, Windows among them.

  Include an encrypted key served by the agent.

Definition of done: every core connection to a VM verifies against the store, the only acceptance of
a new key is create's enrollment, and nothing in Agentworks writes the operator's `known_hosts` for
a VM connection.

## Slice 2: Execution core and the operator exec surface (HLA D1 to D5, D8, D12 to D14; PR A)

- [x] Write [execution-lld.md](./execution-lld.md). It covers:
  - client resolution per workstation;
  - the VM policy (the host policy is slice 4's);
  - the stdin framing and the envelope format;
  - stage 0 and agw-launch's steps, markers and trailer;
  - the terminal handover;
  - request and result types;
  - error mapping;
  - the adoption and deletion list for the proof modules.
- [x] Implement `client.py`, `ssh_process.py` (with stage 0), `envelope.py`, `agw_launch.py`,
      `requests.py` (the items this slice's consumers use), `results.py`, the remaining `errors.py`,
      `access.py`, `attach.py` and `forwarding.py`.
- [x] Delete `execution/carrier.py`, `execution/preparation.py`, `execution/bootstrap.py`,
      `execution/carriers/` and their tests, after adopting what the LLD lists.
- [x] Add the conftest blocker under `cli/tests/execution/`, the static legacy-import check, and the
      recording fake `VMAccess`.
- [x] Migrate `vm exec`, `vm shell`, `agent exec`, `agent shell`, `vm verify-connection` and
      `vm port-forward`. Release probing stays legacy until slice 3, because reinit runs it before
      initialization and must still reach a VM without python3 (`vms/manager/lifecycle.py:962`).
      Keep `vm exec`'s single-argument form as a shell command line, sent as a `Script` under the
      identity's default shell, and several arguments shlex-joined, with a word starting with `=`
      quoted, as today (`vms/manager/exec.py:271`).
- [x] Restore `vm exec`'s several-argument form to today's behavior, shell-joined (a word starting
      with `=` always quoted) and run under the identity's default shell (`vms/manager/exec.py:271`
      on `main`), which the box above first described, wrongly, as a literal argv. Give a departed
      stdout consumer ssh's semantics (close it, keep pumping until the channel ends, report the
      guest's status when its trailer arrives) and end the request when a stderr consumer leaves,
      after delivering held stdout and keeping a trailer already in hand (execution LLD, the pump);
      on Windows the passphrase hint names the absolute path, and the agent check lists keys with
      `ssh-add -L` rather than signing, and only for a `.pub` that holds a type and blob. Test the
      `=` rule under zsh itself, whatever the runner's login shell, and keep one copy each of the
      SSH wire reader (`ssh_identity`) and the rule for a departed reader (`pump.reader_left`). Run
      import's `ssh-keygen` from beside the resolved client, as `ssh-add` is, never from `PATH`.
- [x] Alert where PR A drops a behavior an operator may rely on (operator, 2026-11-01): warn at
      reinit when `/etc/environment`, `/etc/security/pam_env.conf` or the admin's
      `~/.pam_environment` holds settings, and when a VM connection fails before the start marker
      while the configured key is passphrase-protected, hint to load it into the SSH agent. Test the
      pam_env parser over file contents, that reinit warns and never fails on it, and that the hint
      appears only for a passphrase-protected key.
- [x] Type a VM without python3 as `VMNotReady` with a hint naming `agw vm reinit`, whose legacy
      Phase A still runs there, and say so in release notes.
- [x] Limit the admin and agent template `shell` fields to `sh`, `dash`, `bash` and `zsh` at the
      config boundary (HLA D4), with sample-config comments and release notes.
- [x] Add the resolver's verdict to doctor's required-tools group, beside the existing `ssh`, `scp`
      and `tailscale` presence rows, which stay until the legacy paths go (slice 8).
- [ ] Conformance vectors, run against a local sshd and python3 on Linux and on live targets:
  - literal argv, including empty arguments, whitespace and shell characters;
  - script versus stdin, with script source absent from guest argv;
  - every interpreter and startup choice;
  - environment byte-exactness, including `%`, quotes, `${...}`, CR, LF and every other byte but
    NUL;
  - working directory;
  - exit statuses 0, 1 and 255;
  - stage 0 under each supported login shell, with startup files that print and export;
  - output before the start marker dropped;
  - not delivered versus unknown, status 127 typed as `VMNotReady`, and the status of a python3
    without its standard library also typed `VMNotReady`;
  - the refusal statuses: a missing command 127, another `exec` failure 126, a missing working
    directory 125;
  - the kernel size limits: an argument and an environment value at 131,071 bytes run, one more is
    refused, and a total over `ARG_MAX` is refused by `exec`;
  - exact stdin positioning, so the payload reads the caller's first byte;
  - the terminal handover: the timeout, cleanup when the second connection fails, an existing
    directory name refused, and `SigIgn` and `SigBlk` zero in the resulting shell;
  - a startup file that reads stdin, or redirects a standard stream, which leaves the request not
    delivered;
  - signal dispositions and descriptors reset for the payload, and a startup file that overrides
    `TERM` or `XDG_RUNTIME_DIR` leaves the payload's values unchanged;
  - the end marker and trailer under a background child that holds both streams, and with stdout's
    last bytes arriving after the trailer;
  - payload stdout that does not end in a newline, and a `refused` trailer;
  - the deadline;
  - binary stdin and stdout on Windows.
- [x] Unit-test the stream filters under every split and interleaving of the two streams, which
      covers stdout's last bytes arriving after the trailer.
- [ ] Re-prove incident rows I1 to I7, I11 to I14, I19 and I21, I11 with a conformance vector in
      which a bash child of the payload reads no account startup file, run with a pipe and with a
      socket on stdin.
- [x] Amend ADRs 0013 and 0014.
- [x] Audit the entry points this slice moves for incident-derived behavior (their ADRs, linked
      issues and fix history), and add incident inventory rows before deleting anything.
- [x] Collateral: update the command reference and guides for any changed behavior, and
      `cli/agentworks/execution/README.md`.
- [ ] Live evidence:
  - all three workstations against Remote Lima;
  - Linux and macOS against local Lima;
  - Windows against WSL2;
  - one cloud.

  Include a Debian 12 and a Debian 13 guest, and an OpenSSH 8.5 client on one workstation; if no bed
  has one, that cell goes to the operator for disposition.

Definition of done: the migrated commands reach VMs only through `agentworks.execution`, their files
pass the static check, and the package's tests pass with the legacy modules blocked.

## Slice 3: First contact and the VM contract (HLA D10, D11 first half; PR B)

- [x] Write [`contract-lld.md`](./contract-lld.md). It covers:
  - the check table and diagnoses;
  - the provisioning bootstrap additions;
  - `tailnet_address`;
  - Phase A on `VMAccess`;
  - `reachability`, including how it ends a wait at an authentication failure instead of polling it
    into Debian 13 sshd's `PerSourcePenalties`;
  - the operation log record;
  - the vm-platform `contract_version` bump.
- [ ] Add python3 to `PROVISIONING_PACKAGES`, which every platform's bootstrap already installs
      (`lima.py:391`, `wsl2_bootstrap.py:65`, and the AWS, Azure, GCP and Proxmox platforms).
- [ ] Make `ProvisionResult.tailnet_address` required on every platform, and delete
      `ProvisionResult.native_transport` and Phase A's native `tailscale ip -4` fallback:
  - all six platforms settle the address through one shared helper, `settle_tailnet_address`, over
    their own `run_as_root`, retrying so the key is never sent twice and logging the node out over
    that channel when the reads are exhausted (HLA D11). Slice 4's `run_as_root` implementations
    land first in PR B, so the settle is written once, over them;
  - `parse_bootstrap_output` stops requiring an address, and the bootstrap script loses its key and
    its join step in slice 4;
  - `parse_tailnet_address` validates each read and core validates the value again before recording
    it;
  - the provisioning trust file's lifetime narrows to the platform's `create` call.
- [ ] Bump the vm-platform `contract_version` to 2 once, here, with slice 4 landing under the same
      number in the same PR: the kind descriptor, the six platforms, the tests that assert it and
      every "version 1" mention of this kind.
- [ ] Add `UnknownReason.AUTH_FAILED` and its classifier row, typed from
      `Permission denied (publickey` in the client log, and widen the passphrase hint to
      `CONNECTION` or `AUTH_FAILED`.
- [ ] Implement `reachability.py`: `wait(access, *, wording)` polls a fixed `true` probe, polls only
      `CONNECTION`, `TRUNCATED` and `DEADLINE`, and raises every other outcome at once.
- [ ] Move Phase A and release probing onto `VMAccess`:
  - Phase A gets its own module, `vms/initializer/phase_a.py`, holding the fail-closed span and the
    sequence: record the address, first contact in Enroll mode through
    `execution.enrollment_access`, the enrollment post-condition, then the base checks including
    Debian attestation, then `reachability.wait` after `post_tailscale_ready`. `driver.bootstrap_vm`
    stays as the thin caller that hands Phase B a legacy target as today;
  - `COMPLETE` is written after the contract passes, and an interrupt before then marks the VM
    `FAILED` with delete guidance;
  - `vms/manager/release.py` splits into `contract.observe_release` and `reconcile_release`,
    `vm confirm-release` runs on `VMAccess`, and `probe_debian_release` is deleted;
  - `vms/contract.py`, `vms/initializer/phase_a.py` and `vms/manager/release.py` join the static
    legacy-import check.
- [ ] Implement the operation log record: `oplog.format_record`, `VMAccess`'s optional `log`
      callable and `SSHLogger.record`, with redaction left at the logger's one sink. `LOG_DIR`, its
      readers and `SSHLogger` stay until slice 8.
- [ ] Add elevation, with its conformance vectors (E1, E2, E4 and E6), for the contract's sudo
      check, its first consumer: `elevated` on `BoundedRequestOptions`, `stage0_command(elevated=)`,
      and `PermissionDenied` for an agent-bound access, derived from the access's identity. An
      elevated attach is excluded by the types. Prove an elevated request on a fresh,
      never-initialized Debian 12 and 13 VM.
- [ ] Add `vms/contract.py`: the `(id, severity, probe)` table, the script protocol and its parser,
      `observe_release`, `check_base` and `enforce`. Run the base checks first at reinit, moving
      slice 2's reinit pam_env warning into them, and add `vms/contract.py` to the package guard
      (HLA D14).
- [ ] Amend ADR 0025.
- [ ] Tests:
  - a VM failing a base check is not reported ready, and a warning-only report does not fail;
  - a platform that cannot report the address logs the node out over its provisioning channel and
    fails create, without a second join, and names the node when the logout fails;
  - a VM with pam_env settings gets a warning naming the file and still passes the contract;
  - Phase A marks the VM `FAILED` and secures it on a failure or an interrupt before the contract
    passes, and leaves `COMPLETE` once it has;
  - `reachability.wait` stops at the first authentication failure.
- [ ] Audit the entry points this slice moves for incident-derived behavior (their ADRs, linked
      issues and fix history); rows I22 to I28 are in the HLA inventory.
- [ ] Collateral: platform author documentation for `tailnet_address` and contract version 2, and
      the command reference for create and reinit diagnoses.
- [ ] Live evidence: create and reinit on every platform from at least one workstation each.

Slice 3 does not merge without slice 4: between them a VM without python3 cannot be reinitialized
and the `VMNotReady` hint is circular, and no stopgap wording is added in between.

Definition of done: core's work on a new VM starts over SSH at the reported address, its first
connection enrolls the host key, and the base contract check follows, release first.

## Slice 4: Recovery and the platform surface (HLA D11 second half; PR B)

Line numbers in this slice are at `60d347220`, the platform LLD's baseline.

- [x] Write [`platform-lld.md`](./platform-lld.md). It covers:
  - `run_as_root` and `RawRun`, and how each channel bounds its output and its wait by the deadline;
  - core's `root_script` wrapper and `RootScriptResult`;
  - `read_host_keys` and its cloud overrides, with no provider-side fingerprint source;
  - `recovery_needs_guest_sshd` and `native_shell`;
  - each platform's root channel and private access, `public_ssh.py` under the VM policy, and the
    host policy for Remote Lima;
  - the repair, rekey and logout scripts, and `vm recover`;
  - delete's refusal to log out, under Unverified trust, a cloud VM that never enrolled a host key.
- [ ] Implement `execution/root_script.py`: `RawRun`, `RootScriptResult`, the wrapper with its one
      embedded secret, `run` with its optional operation log, `run_process` over
      `PipeSession(nonce=None)`, and `read_host_keys`. `spawn` raises `ConnectivityError` naming a
      program other than OpenSSH that cannot start, and `trust.fingerprint` lands.
- [ ] Implement `run_as_root` on every platform before slice 3's address settle, so the settle is
      written once over it:
  - `lima_host.py` for Lima and Remote Lima, with Remote Lima's host-side detached create;
  - WSL2 through `wsl --exec /bin/bash -s`, with a `_WSL` program constant shared with `_wsl`;
  - `plugins/proxmox/guest_agent.py`, replacing `plugins/proxmox/transport.py`;
  - `public_ssh.py` for the clouds under the VM policy, with each cloud's `read_host_keys`.
    `run_as_root` and `recovery_needs_guest_sshd` become descriptor requirements, and
    `ProvisionRequest.ssh_private_key` becomes `Path`.
- [ ] Implement `build_host_argv` (HLA D2) in `ssh_process.py` for Remote Lima's host connection,
      its only caller, and refuse whitespace and control characters in `LimaSshPlacement.host`.
- [ ] Port `tailscale_join.py` onto `run_as_root`: `JOIN_FRAGMENT` through
      `--auth-key=file:/dev/stdin`, `join_tailscale`, `read_tailnet_address`,
      `settle_tailnet_address` with its `rerun_hint`, and `EphemeralTailscaleBootstrap`. WSL2 and
      Proxmox run a key-free bootstrap through `run_as_root` and then join. Delete the bootstrap
      generator's key parameter and step 7, `TAILSCALE_JOIN_STDIN_COMMAND` and the staging files,
      and re-prove incident row I10 with a test.
- [ ] Make `transient_route` and `probe_failure_hint` private to each cloud. Add explicit imports of
      `public_ssh.py` and `vms/recovery.py` to the package guard, and the plugin and migrated files
      the platform LLD lists to the static check (HLA D14).
- [ ] Add `vms/recovery.py` with the repair, rekey and logout scripts, and move rekey and delete's
      logout onto them, rekey with an operation log that registers the auth key. Make `vm start` and
      the activation gate start the VM, wait for it to answer over SSH, and fail with a hint naming
      `agw vm recover` when it does not, as the operator ruled on 2026-10-31 and 2026-11-01. Both
      waits use `reachability.wait`. Delete `vm start`'s rejoin and its conditional auth-key read
      (`vms/manager/power.py:85-120`), and `rejoin_tailscale` with its exports.
- [ ] Delete the legacy `TailscaleWait` and `wait_for_reconnect` (`transports/__init__.py:248`,
      `:279`) with their last callers, which move to `reachability.wait` in this slice.
- [ ] Add `agw vm recover <vm>`, with its operation log:
  - refuse a VM whose provisioning failed, a stopped VM, and, worded separately, a VM whose power
    state is unknown;
  - host-key re-establishment first when the record is missing;
  - the repair script, then the address read, settling only when the repair joined, then one wait.
    Repoint `VMNotReady` and `TrustMissing` guidance at it.
- [ ] Delete the activation gate's lazy repair-secret protocol, which only its rejoin used:
      `GateTarget.repair_secret_refs`, `_GateSecrets`, `Resolver.resolve_late_repair`, and the call
      sites in `vms/nodes.py` and `sessions/manager/_scope.py`. Update the collateral that names it,
      including the `agentworks-reviewer` persona's sanctioned conditional-repair shape.
- [ ] Move `vm shell --platform` onto `native_shell`, with `FeatureUnavailable` for a platform that
      has none and a refusal of `--workspace` with `--platform`, and delete
      `vms/manager/platform_shell.py`.
- [ ] Delete `VMPlatform.native_transport`, the `transports.native_transport` factory, the legacy
      platform transports (`transports/lima.py`, `remote_lima.py`, `wsl2.py`, `_shared.py`) and
      `plugins/proxmox/transport.py`, with their tests.
- [ ] Tests, as the platform LLD lists them:
  - the wrapper: the status only when both closing markers arrive, otherwise unknown, the secret in
    no field, `repr`, error or log record, and one dispatch however it ends;
  - `run_process`'s deadline, output limit and background child, and its error for a missing
    program;
  - the argv: `PublicSSH` ignoring operator configuration and the host argv honoring it;
  - the address read and settle, including that only the settle logs out;
  - the auth key in exactly one script on each of the six creates (I10);
  - the commands: start and the gate failing with the recover hint and resolving no secret, recover,
    rekey, delete's logout outcomes, and `vm shell --platform`'s refusals before the boundary;
  - conformance vectors W1 to W9.
- [ ] Audit the entry points this slice moves for incident-derived behavior (their ADRs, linked
      issues and fix history); rows I29 to I34 are in the HLA inventory.
- [ ] Collateral, as the platform LLD lists it: ADRs 0003 and 0019, platform author documentation,
      the command reference, the READMEs, the sample config, completions, the guides (including the
      Proxmox guide's start-time rejoin note, `docs/guides/proxmox.md:201-205`, and the
      troubleshooting topic), the reviewer persona, and the stale docstrings and comments.
- [ ] Live evidence:
  - create on every platform, with the key-free bootstrap's output size recorded on WSL2 and
    Proxmox;
  - rekey, delete's logout and `vm recover` on every platform from at least one workstation each;
  - `vm start` after the node is removed, and after an ephemeral key's node expired;
  - recovery of a broken sshd on WSL2 and on Proxmox;
  - recovery of a lost store record on one cloud and on Remote Lima;
  - the V cells of vectors W1 and W3 to W9.

Definition of done: core reaches a native channel only through `run_as_root` or `native_shell`, and
no platform's private access imports a legacy module.

## Slice 5: Files and capability delivery (HLA D6, D12; PR C)

- [ ] Write `files-lld.md`. It covers:
  - each operation's Python program and its use of stdin and stdout;
  - the `flock` protocol;
  - the symlink policy;
  - mode and ownership;
  - cleanup.
- [ ] Implement `PythonProgram`, `files.py` and the file program, finite stdin for `run` with
      `Bytes(data, sensitive=)`, and `RunContext.admin_access()` and `agent_access()`.
- [ ] Make the operation log keep the streams of a request with a sensitive stdin out entirely (HLA
      D13), now that `Bytes(data, sensitive=)` gives it a first caller.
- [ ] Migrate:
  - artifact publication and session artifacts;
  - native probing;
  - the harness-native settings merge and CLI transcript reads;
  - the codex and claude artifact reads;
  - harness integrations' readiness and start stages;
  - the git-credential providers.
- [ ] Tests:
  - `write` applies mode and owner before the rename;
  - `write_if` and `remove_if` detect a concurrent writer, including a tree upload replacing the
    directory above the target;
  - JSON null survives an update;
  - a malformed destination is refused;
  - a symlinked destination is refused;
  - cleanup touches only what the operation created;
  - `preserve_metadata=True` keeps the existing file's owner, group, mode and ACLs;
  - a sensitive stdin is absent from logs and exceptions (a conformance vector).
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
- [ ] Add the `observation` request option (a caller's declaration that a request only reads, so it
      may be dispatched again after `NotDelivered` or `Unknown`) with its first callers,
      `detached.observe` and the sessions' status probes (HLA D5).
- [ ] Migrate:
  - workspace backends, rehome and repair;
  - workspace copy, using `run` over an opaque tar stream;
  - `sessions/`, including tmux, consoles, status probes and pid repair, with session environment in
    the request and console panes taking theirs from the terminal handover, so no `tmux -e` argument
    carries a value (HLA D8). Every path that creates or recreates a pane (adding a shell, console
    restore, console recovery after a killed attach) parks a fresh handover;
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
      grant (`_write_sudoers_console_setenv`), with reinit removing the installed fragment from
      existing VMs.
- [ ] Move the session callers of `_assert_agent_ssh_works` (`sessions/manager/_create.py`,
      `_create_roll.py`, `_pids.py`) to `_assert_agent_access_works`, and delete the legacy probe.
- [ ] Re-prove incident rows I6a and I8.
- [ ] Supersede ADR 0017.
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
- [ ] Move `LOG_DIR` and the operation log's readers (`doctor_state.py`, `vms/manager/lifecycle.py`,
      `vms/manager/_helpers.py`, `vms/manager/power.py`, `vm logs`) and their test patch sites to
      `execution/oplog.py` as `SSHLogger` is deleted, matching log files by the exact
      `<vm>-<timestamp>-<stem>.log` shape so VM `dev` never matches VM `dev-2`'s logs, and update
      the `LOG_DIR` mention in the `transports/ssh.py` docstring.
- [ ] Delete the remaining legacy modules with their tests (slice 4 deletes the sixth,
      `agentworks.plugins.proxmox.transport`):
  - `agentworks.transports`
  - `agentworks.ssh`
  - `agentworks.remote_exec`
  - `agentworks.harness_setup.runner`
  - `agentworks.native_files`
- [ ] Delete `RunContext.admin_target()` and `agent_target()`, and the `SSHError` special case in
      `cli/_entry.py`.
- [ ] Delete doctor's `ssh` and `scp` `PATH` presence rows, which the resolver's verdict replaces.
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
