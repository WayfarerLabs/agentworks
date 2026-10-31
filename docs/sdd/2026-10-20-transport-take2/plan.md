# Implementation Plan: Transport Take 2

- Status: Design under review; no implementation started
- Date: 2026-10-31
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Source baseline: `eee56117c`
- Delivery: one design PR (HLA and plan), then one PR per slice below, each based on `main`

## Delivery rules

- Each slice is one PR that is complete on its own terms: it ships a new piece together with every
  consumer the slice names, the collateral that piece outdates, and live evidence for the cells the
  slice lists. No slice lands surface ahead of its consumers (R10).
- Each slice starts by writing or updating its LLD in this directory and getting it through the
  private review loop before code. LLDs are linked from the slice's first checkbox.
- Every slice runs the private quality loop (`agentworks-reviewer`, `muntz`, and a generic
  correctness pass for code) before it goes ready, and live validation through the
  `integration-testing` skill before merge. Missing live coverage goes to the operator for
  disposition and never counts as passing (R11).
- Add each slice's migrated modules to the independence guard (HLA D14) in the same PR.
- The operator owns merge.

## Phase 0: Design

- [x] Scout the consumer inventory, platform surfaces, SSH mechanics and predecessor proof code at
      `eee56117c`.
- [x] Write the HLA and this plan.
- [ ] Run the private project review and complexity pass on the design and resolve material
      findings.
- [ ] Publish the design PR, address published feedback, and get operator answers to the HLA's open
      questions.

Definition of done: the HLA and plan are on `main`, and the HLA's open questions have recorded
answers.

## Phase 1: Trust (HLA D9)

- [ ] Write `trust-lld.md`: store layout and alias derivation, enrollment, import matching for plain
      and hashed records and markers, re-establishment hook, removal, managed alias options, and the
      legacy argv changes.
- [ ] Implement `execution/trust.py` with the store, enrollment reset, import and removal.
- [ ] Add `agw vm import-host-keys [--known-hosts PATH] [VM...]`, using the name the operator
      confirmed.
- [ ] Point every legacy core SSH and scp path at the store with `StrictHostKeyChecking=yes`:
      `ssh.py`, `transports/ssh.py`, `vms/backup.py`, `workspaces/manager/copy.py` and the port
      forward in `vms/manager/tailscale.py`.
- [ ] Make `vm create`'s first core connection enroll into a freshly reset store file.
- [ ] Make `vm delete` remove the store file after the platform delete succeeds.
- [ ] Add `HostKeyAlias`, `UserKnownHostsFile` and `StrictHostKeyChecking yes` to managed `awvm--`
      and `awagent--` entries.
- [ ] Give cloud provisioning SSH a throwaway known-hosts file per create, so nothing in core or
      cloud plugins writes the operator's `known_hosts`.
- [ ] Tests: import over plain, hashed and marked records; a missing record is `TrustMissing` and
      never first use; enrollment only from create; alias entries render the store options; the
      source known-hosts file is byte-identical after import.
- [ ] Collateral: command reference, completions, operator guide for the one-time import, doctor
      check for VMs without a store record, release notes.
- [ ] Live evidence: create, operator `ssh awvm--<vm>`, import of an existing VM and a tailnet
      address change after rekey. Cover Remote Lima from macOS and Linux, AWS from Windows, and
      WSL2. Include an encrypted key served by the agent.

Definition of done: no Agentworks code path accepts a host key except create's enrollment, every
core SSH connection verifies against the store, and the operator's `known_hosts` is never written.

## Phase 2: Execution core and the operator exec surface (HLA D1 to D5, D8, D12 to D14)

- [ ] Write `execution-lld.md`: client resolution per workstation, the policy file, the envelope
      format and bound, the bootstrap steps and trailer, request and result types, error mapping,
      logging, and the adoption and deletion list for the proof modules.
- [ ] Implement `client.py`, `policy.py`, `process.py`, `envelope.py`, `bootstrap.py`,
      `requests.py`, `results.py`, `errors.py`, `logging.py`, `access.py`, `terminal_attach.py` and
      `forwarding.py`.
- [ ] Delete `execution/carrier.py`, `execution/preparation.py`, `execution/carriers/` and their
      tests, after adopting what the LLD lists.
- [ ] Add the in-process fake `VMAccess` for manager tests.
- [ ] Migrate `vm exec`, `vm shell`, `agent exec`, `agent shell`, `vm verify-connection`, release
      probing (`vms/manager/release.py`) and `vm port-forward`.
- [ ] Conformance vectors run against a local bootstrap and on live targets:
  - literal argv, including empty, whitespace and shell characters;
  - script versus stdin;
  - every interpreter and startup choice;
  - environment byte-exactness, including `%`, quotes, `${...}`, CR, LF and any other byte but NUL;
  - working directory;
  - elevation, and agent elevation refused;
  - exit statuses 0, 1 and 255;
  - delivery failure versus unknown;
  - the trailer under a background child;
  - the deadline;
  - the size bound refusal;
  - a sensitive stdin absent from logs and exceptions;
  - binary stdin and stdout on Windows.
- [ ] Re-prove incident rows I1 (closed stdin, no forced TTY) and I2 (Windows byte-exact stdin)
      against the new stack.
- [ ] Amend ADR 0014.
- [ ] Collateral: `agw doctor` reports the client path, build and version; command reference and
      guide updates for any changed behavior.
- [ ] Live evidence: all three workstations against Remote Lima, Linux and macOS against local Lima,
      Windows against WSL2, and one cloud. Record the Windows 10 inbox client version, if a Windows
      10 host is available.

Definition of done: the migrated commands use only `agentworks.execution`, and their modules pass
the independence guard.

## Phase 3: VM contract and platform surface (HLA D10, D11)

- [ ] Write `platform-lld.md`: `ProvisionResult` changes, `run_recovery` and `RecoveryResult`, the
      sshd-dependence declaration, per-platform recovery channels and private access, the recovery
      scripts, `vm recover`, and the contract probes and diagnoses.
- [ ] Add the AcceptEnv fragment to the shared provisioning bootstrap script.
- [ ] Make `tailnet_address` required. Delete `ProvisionResult.native_transport`,
      `VMPlatform.native_transport` and the `transports.native_transport` factory.
- [ ] Implement `run_recovery` for each platform with its private channel. Rehome the Lima, Remote
      Lima, WSL2 and Proxmox native code as plugin-private modules (`plugins/proxmox/guest_agent.py`
      replaces `plugins/proxmox/transport.py`). Port `tailscale_join.py` and `wsl2_bootstrap.py` off
      `ExecTransport`.
- [ ] Implement `recovery_ssh.py` for the cloud platforms, verifying against the VM's store entry.
- [ ] Move rejoin, rekey and delete-time logout onto `run_recovery`, keeping the activation gate's
      automatic rejoin.
- [ ] Add `vms/contract.py` with the base and initialized probes. Run the base probe as create's
      first connection and first at reinit, and the initialized probe at the end of Phase B.
- [ ] Move Debian attestation onto the base probe and amend ADR 0025.
- [ ] Add `agw vm recover <vm>` with access repair, rejoin, host-key re-establishment and SSH
      verification. Keep `vm shell --platform` on platforms that offer a native shell, with a typed
      refusal elsewhere (R2).
- [ ] Tests: each platform declares the sshd dependence; recovery returns status, delivery failure
      or unknown; logout reports the script's status or unknown and never checks SSH afterwards; a
      VM without the fragment gets `VMNotReady` naming recovery.
- [ ] Collateral: platform author documentation for the new surface, command reference, completions,
      operator guide for recovery.
- [ ] Live evidence: create, reinit, rekey, delete and recovery on every platform, from at least one
      workstation each. Include recovery of a VM with a broken sshd on WSL2 or Proxmox, and with a
      lost store record.

Definition of done: no core code reaches a native channel except through `run_recovery` or the
optional native shell, and every platform's private access imports no legacy module.

## Phase 4: Files and capability delivery (HLA D6, D12)

- [ ] Write `files-lld.md`: each operation's script, stdin and stdout use, the `flock` protocol,
      tree staging and swap, mode and ownership, and cleanup rules.
- [ ] Implement `files.py` and `RunContext.admin_access()` and `agent_access()`.
- [ ] Migrate artifact publication, session artifacts, native probing, the harness-native settings
      merge and CLI transcript reads, codex and claude artifact reads, harness integrations'
      readiness and start stages, and the git-credential providers.
- [ ] Tests: publish applies mode and owner before rename; `publish_if` detects a concurrent writer;
      JSON null survives a merge; a malformed destination is refused; cleanup touches only what the
      operation created.
- [ ] Live evidence: harness setup and a session start with artifacts, on Remote Lima from macOS and
      on one cloud from Windows.

Definition of done: `native_files.py` has no remaining callers.

## Phase 5: Initialization

- [ ] Migrate the VM initializer (`vms/initializer/`), `vms/hardening.py`, `vms/tailscale_dns.py`,
      `sources.py`, `command_checks.py`, `git_config.py`, `git_credentials/`, the agent initializer
      and grants, and the harness setup runners. The `SetupRunner` environment rules become request
      environment on the admin or agent access.
- [ ] Replace hand-written `printf | sudo tee` and `write_file` plus `chmod` sequences with
      `files.write`.
- [ ] Live evidence: create and reinit on every platform from at least one workstation each.

Definition of done: create and reinit run entirely on the new transport.

## Phase 6: Workspaces and sessions

- [ ] Migrate workspace backends, copy (replacing its hand-built ssh tar stream with
      `download_tree`), rehome and repair, and `sessions/`, including tmux, consoles, status probes
      and pid repair. Session environment travels in the request rather than in tmux `-e` arguments
      where tmux inherits it.
- [ ] Live evidence: session and console create, attach, detach and restore on Remote Lima from all
      three workstations and on WSL2.

Definition of done: no workspace or session module imports a legacy module.

## Phase 7: Backup and detached work (HLA D7)

- [ ] Implement `detached.py`.
- [ ] Migrate backup's launch, observation, interrupt and download, replacing its hand-built scp
      with `download` to a file sink with progress.
- [ ] Tests: a fresh launch never matches an earlier one; observation resumes across a dropped
      connection; interrupt stops the work.
- [ ] Live evidence: backup with a forced disconnect and with an interrupt, on WSL2 (idle shutdown)
      and Remote Lima.

Definition of done: `remote_exec.py` has no remaining callers.

## Phase 8: Removal

- [ ] Re-prove every incident inventory row against the new stack and record the evidence here.
- [ ] Delete `agentworks.transports`, `agentworks.ssh`, `agentworks.remote_exec`,
      `agentworks.harness_setup.runner`, `agentworks.native_files` and
      `agentworks.plugins.proxmox.transport`, with their tests.
- [ ] Delete `RunContext.admin_target()` and `agent_target()`, and the `SSHError` special case in
      `cli/_entry.py`.
- [ ] Confirm by search that no core code builds its own `ssh` or `scp` command line outside
      `agentworks.execution` and platforms' private access.
- [ ] Complete the 18-cell evidence matrix, sending any gap to the operator.
- [ ] Promote the transport's lasting concepts to permanent docs (an architecture page for
      `agentworks.execution`, platform author guidance), then write `locked.md`.

Definition of done: R10's end state holds, every matrix gap has an operator disposition, and the SDD
is locked.
