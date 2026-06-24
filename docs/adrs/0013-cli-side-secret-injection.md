# 13. CLI-side Secret Injection for VM Shells

Date: 2026-06-06

## Status

Accepted

## Context

The env-and-secrets SDD introduces a general mechanism for propagating environment variables and
secrets into the shells that agentworks opens on its managed VMs. Secret _values_ have to enter the
shells somehow. There are several plausible places to put them:

1. **On the VM filesystem**, written once at resource-create time, read by the shell on each start.
2. **Brokered on the VM**, mediated by a long-running service that hands out secrets per-session.
3. **Injected by the CLI**, sourced from operator-side env or interactive prompt at command time,
   then exported inline at shell open.

These differ along three axes that matter:

- **Persistence**: how long secret values live on the VM.
- **Isolation**: which subjects on the VM can read which secrets.
- **Complexity**: how much VM-side machinery the design requires.

Agentworks's threat model has two distinct surfaces. The **operator's workstation** is the trust
anchor: it holds personal vaults (1Password, keychain, etc.), the SSH keys to every VM, and the CLI
process itself. The **VMs** are downstream of that trust: they run agentic workloads, may be exposed
to prompt-injection vectors via the agent itself, and are the more-likely site of a runtime
compromise. Decisions that shift secret material away from the VM and toward the workstation
generally improve the overall posture, because the workstation already has to be secured for
everything else.

The original instinct for this work leaned toward the on-disk file model: it preserves the existing
"secret known only at create time" property (the current `git_credentials` flow writes once to
`~/.git-credentials` and is done). Closer analysis surfaced security and isolation properties of the
CLI-injection model that strongly outweigh this benefit.

## Decision

Agentworks injects secret values into VM shells from the **CLI side at command time**. The CLI
process resolves secret values from operator-side sources (env var, then prompt) and hands them to
the SSH layer as a single coalesced `-o SetEnv="K1=V1" "K2=V2" ...` argument per ADR 0014; the
remote sshd places them in the spawned shell's environment before exec, so values never persist on
the VM. No VM-side storage of secret material, no VM-side broker process.

(This ADR was originally drafted around a CLI-composed `build_export_block` prelude. Phase 3 of the
SDD pivoted to SSH SetEnv, which is documented in ADR 0014; the trust-anchor analysis below is
unchanged by that pivot: the CLI is still the only place secret values exist outside of running
processes.)

Per FRD R5 / HLA "Identity vars on the VM", user-defined env (plaintext or secret) is never cached
on the VM. The on-VM profile fragments hold only the VM-stable identity vars (`AGENTWORKS_VM`,
etc.); user-defined env is always computed at command time and injected inline at the shell-open
site.

## Consequences

### Positive

1. **No secret persistence on the VM.** Secrets exist only in the address space of currently running
   processes and in the brief SSH command line that started them. An attacker who gains VM access
   later (post-compromise reconnaissance, stolen snapshot, leaked backup, image reuse) finds
   nothing. The file model would leak credentials indefinitely after the first write.

2. **Genuine per-session isolation within an agent.** Agent-mode sessions each run their own tmux
   server (separate process tree, separate socket). Each server's initial environment is
   independent: a process under `tmux -S /tmp/socket-a` cannot see `tmux -S /tmp/socket-b`'s env
   vars. In the file model, every session for one agent runs as the same Linux user, so any session
   can read any other session's secret files. The env model gives real per-session isolation that
   the file model cannot replicate without a broker.

3. **Workstation-anchored credential management.** The operator's vault (1Password, keychain, etc.)
   is the only place secret values are stored long-term. The VM never has its own vault problem to
   solve. This matches where the trust anchor already is.

4. **Minimal VM-side machinery.** No daemon to install, supervise, audit, or upgrade. No privileged
   service to harden. No session-identification protocol to design. The VM-side contract is just
   "shells receive their env on stdin."

5. **Uniform mechanism across surfaces.** Every shell-opening site (provisioning, session create,
   console add-shell, exec, etc.) uses the same SSH SetEnv plumbing (the SSH layer's `env=` kwarg
   coalesces every pair into one `-o SetEnv="K1=V1" "K2=V2" ...` arg). The transport is the SSH
   command line, which already exists everywhere agentworks opens a shell.

6. **No rotation or refresh semantics required.** Every shell-open is a fresh resolution from the
   configured backend chain. There is no "resolved at create time, refreshed every N hours"
   machinery (which Kubernetes External Secrets Operator and similar systems need because they
   materialize values into long-lived stores). Operator rotates a secret in their vault → the next
   shell-open picks up the new value automatically. Existing shells retain the env they captured at
   create time, consistent with FRD R5 "Attach inherits create-time env" and the broader "restart to
   pick up new values" contract.

### Negative

1. **The CLI handles secrets on every invocation that opens a shell.** In the file model, the
   create-time command was the only place secrets had to be known; later commands that open new
   shells (`session restart`, `console add-shell`, `agent exec`, `vm exec`, etc.) didn't need them.
   With CLI injection, every such command needs the secret available through the active backend
   chain. (`session attach` is unaffected: it joins the existing tmux server's captured env, no
   re-resolution.) In practice operators wrap their shell with `op run --` or equivalent so
   credentials are present any time `agw` runs, but it is a real cost. The CLI process handles
   secret material more often, on the (presumed-trusted) operator workstation rather than the
   (less-trusted) VM. Net acceptable for agentworks's use case, but worth naming.

2. **SSH command line exposure window.** The `export KEY=val && cmd` prelude appears on the SSH
   command line, which is briefly visible via `ps` to any process that can read it during the start
   window. On the VM this is bounded to the same uid the shell will run as (other uids cannot read
   `/proc/<pid>/cmdline` of unrelated users without privilege), so the exposure is no worse than the
   env vars themselves once the shell is running. This is the same exposure the current
   session-template `env` mechanism has; the ADR neither introduces nor improves on it. Operators
   with stricter requirements may need to consider not putting their most sensitive secrets through
   a shell-driven CLI at all.

3. **No within-uid isolation.** Within a single Linux uid, `/proc/<pid>/environ` is readable by the
   owner: it reflects the initial environment captured at exec time. A program can scrub its own env
   after start, but the file model has the same property (a single uid can read its own files).
   Within-uid isolation is not a property the env model provides; do not lean on it in security
   arguments. The isolation gains over the file model come from per-session process separation
   (positive #2) and from non-persistence (positive #1), not from any magic about env vars.

4. **Resolver and prompting complexity moves into the CLI.** The CLI gains a `SecretResolver`, an
   eager-prompting orchestration step, a static-vs-dynamic-filter analysis for computing the
   candidate secret set, and a non-interactive failure mode. This is real complexity, but it lives
   on the workstation side of the trust boundary where it can be covered by ordinary unit tests and
   operator UX work. The alternative (broker) would have put comparable complexity on the VM side,
   where it would be harder to audit and harder to update.

5. **Cold-start cost.** Each command that opens a shell does a fresh resolve. Repeated commands in
   the same shell session re-resolve. The in-process resolver cache amortizes within a single
   command, but not across commands. Mitigated entirely by env-var-backed resolution (no prompt if
   `AW_SECRET_*` is set), which is the expected operator workflow.

### Neutral / not a tiebreaker

- **Same-uid leakage.** As noted in negative #3, within a single Linux user both models leak in
  roughly equivalent ways. The CLI-injection model does not win on this axis; it wins on the
  cross-session and persistence axes.

## Alternatives Considered

### A: Secrets persisted on VM disk

Write each secret to a file on the VM at create time, with mode 0600 and ownership scoped to the
consuming user. Read by the shell at start time via profile fragment, dotfile inclusion, or similar.

**Why considered:**

- Preserves the "secret known only at create time" property that current `git_credentials` has.
- Mechanism is well-understood (git-credentials, ssh keys, kubeconfig, .pgpass, etc. all use this
  model).
- Easy to author: shell already knows how to source files.

**Why rejected:**

- **Persistence is the wrong default.** Once written, the secret is on disk for the life of the
  resource. Any future attacker who reaches the VM (compromised agent, snapshot leak, lateral
  movement from another tenant, etc.) finds it. The window of exposure is unbounded.
- **No per-session isolation.** A workspace's secret file is readable by every session for that
  workspace's agent. The agent user can trivially `cat` across sessions. In the env model, separate
  session processes get separate envs.
- **VM-side migration burden.** Rotating a secret means writing new files to every VM, ideally
  atomically. With CLI injection, rotation is just "update the operator vault"; the next shell-open
  picks it up.
- **Encrypted-at-rest doesn't help much.** SOPS/age-style approaches solve the "secrets-in-config"
  problem but still require a key to be on the VM to decrypt at read time; that key is itself a
  secret on disk.

### B: VM-side secret broker

Run a privileged service on each VM that holds (or proxies) secrets and hands them out per request,
authenticated by Unix socket peer credentials (PID, UID) or a `setuid` helper. Shells ask the broker
for their secrets at start.

**Why considered:**

- Achieves per-session isolation that the file model lacks (broker can mediate).
- Centralizes secret storage on the VM, so rotation could be done in one place.
- Allows lazy / on-demand secret materialization (broker could pull from upstream vault when asked).
- Conceptually elegant; mirrors patterns like systemd `LoadCredential` or HashiCorp Vault's agent.

**Why rejected:**

- **End state is identical.** Once the broker hands the secret to the shell, the secret is in
  process environment exactly as it would be in the CLI-injection model. The broker doesn't improve
  the post-handoff exposure; it just moves the trust boundary.
- **Session identification is still hard.** The broker needs to know which session is asking, so it
  can decide which secrets to release. Peer-credential checks (`SO_PEERCRED`) get the uid and pid;
  mapping pid to "which session" requires either a registration protocol or stuffing identity into
  the shell launch some other way, which is exactly the problem we'd be using the broker to avoid.
- **VM-side storage problem reappears.** The broker has to get the secret from somewhere. Either it
  pulls from an upstream vault (now the VM needs vault credentials, which is itself the secret-on-VM
  problem) or operators push secrets to it (now the broker is a write target with its own access
  control story).
- **Operational cost.** A long-running privileged service per VM: install, supervise, monitor,
  upgrade, harden. A compromise of the broker is a compromise of every secret it has ever brokered.
  The blast radius of broker compromise is meaningfully larger than CLI compromise: the broker can
  be attacked from any process on the VM, the CLI can only be attacked on the operator workstation.
- **Cost / benefit imbalance.** Substantial new infrastructure (daemon, socket protocol, ACLs, audit
  trail, rotation story, monitoring) for benefits the CLI-injection model already provides via much
  simpler machinery.

### C: Hybrid (broker as opt-in second source)

The HLA's `SecretSource` protocol already accommodates additional providers being added later
(keychain, 1Password CLI, Vault, etc.). A VM-side broker could be added as a `BrokerSource` in a
future iteration if a use case emerges.

**Why considered:**

- Preserves the simple CLI-injection default while leaving the door open for VM-side options.

**Why not chosen now:**

- Premature. No concrete use case justifies the complexity yet. The protocol shape already allows
  adding it later if one materializes.

## Future shape

A likely future direction is a long-lived operator-side controller process that mediates between the
CLI (or other clients) and the VMs, with its own access to secret sources (vaults, keychains). That
direction does not change this ADR's core decision: the trust anchor stays on the operator side, and
no secret material is persisted on the VM. The controller becomes the new "CLI" for the purposes of
this ADR's reasoning. The `SecretSource` protocol shape is what makes the transition mechanical: the
controller adds vault-backed sources, the prompt fallback drops away in favor of API errors to
clients, and the resolver caching model gets revisited for the longer process lifetime. None of that
requires reopening the core decision documented here.

## See also

- [SDD: env-and-secrets](../frd.md): functional requirements
- [SDD: env-and-secrets HLA](../hla.md): high-level architecture
- [ADR 0003: Tailscale for VM networking](../../../adrs/0003-use-tailscale-for-vm-networking.md) --
  related trust-anchor reasoning
- [ADR 0007: Separate agent identity from task execution](../../../adrs/0007-separate-agent-identity-from-task-execution.md)
  related isolation reasoning
