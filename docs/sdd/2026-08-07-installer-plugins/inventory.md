# Installer and setup inventory

- Status: Draft for R1 review
- Date: 2026-08-08
- Scope: VM provisioning, repeatable VM initialization, and the matching agent-user setup paths
- Inputs: the [FRD](./frd.md), the initializer code at `75c6edd`, and the plugin and capability
  contracts already on `main`

## Classification rule

A step is **core-essential** when Agentworks cannot provide its supported VM, account, workspace,
session, security, or connectivity contract without it. Core retains the orchestration and the
smallest bootstrap needed to reach that state.

A step is **plugin-bound** when it installs or configures an optional toolchain through an external
mechanism whose dependency, configuration family, and failure modes are distinct from core. A
plugin-bound step can still be widely used. Popularity does not make a mechanism fundamental to what
an Agentworks VM or user is.

The classification covers execution, not only files under `vms/initializer/`. Admin and agent setup
duplicate several mechanisms, so moving only the VM-side helper would leave the same mechanism in
core and fail the purpose of the effort.

## Current execution topology

VM create has two phases. Phase A provisions a reachable VM and is fatal on failure. Phase B runs
after Tailscale SSH is available and is repeatable through `vm reinit`; most step failures become a
partial initialization rather than making the VM unreachable (`vms/initializer/driver.py:70-300`).
Agent creation has its own user setup pipeline in `agents/initializer.py`.

The ordering is part of behavior parity. In particular:

- Phase A must establish the account, minimal packages, SSH, hostname, swap, platform safeguards,
  and Tailscale before Phase B can run (`capabilities/vm_platform/bootstrap_script.py:70-239`).
- Core apt packages must be present before identity and skeleton files are written because apt may
  replace package-owned shell files (`vms/initializer/driver.py:617-644`).
- Admin mise configuration precedes dotfiles, while the explicit lockfile follows dotfiles and git
  credentials; mise installation follows all three (`vms/initializer/driver.py:733-802`).
- User install commands follow mise, and the final Agentworks shell-source repair follows every
  user-owned installer (`vms/initializer/driver.py:804-838`).

## Exhaustive step inventory

The tables inventory every remotely mutating or lifecycle-significant step in the three setup
pipelines. Helper-internal actions that form one atomic contract stay together, such as writing an
apt key and its matching source list. The classification is nevertheless per invoked setup step, not
per broad subsystem.

### Phase A: VM bootstrap and connectivity

| ID  | Step in execution order                                                                       | Classification | Rationale                                                                                                                                           |
| --- | --------------------------------------------------------------------------------------------- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| A01 | Create a missing admin user, private group, home, and bash shell; always converge sudo access | Core-essential | Establishes the control identity used by every later operation. Existing-user shell/home convergence and the primary-group check happen in B18-B19. |
| A02 | Seed admin `.bashrc` and `.zshrc`                                                             | Core-essential | Prevents shell first-run behavior and gives the control identity a usable baseline.                                                                 |
| A03 | Update the distribution and install the minimal provisioning package set                      | Core-essential | Installs only what is needed to make the VM reachable and able to join the core transport.                                                          |
| A04 | Preserve SSH host keys across cloud-init boots                                                | Core-essential | Prevents the supported SSH identity from changing on restart.                                                                                       |
| A05 | Install the operator's authorized SSH key                                                     | Core-essential | Makes the VM accessible for recovery through the operator identity.                                                                                 |
| A06 | Configure swap                                                                                | Core-essential | Applies the VM template's machine-level provisioning contract.                                                                                      |
| A07 | Set the persisted hostname                                                                    | Core-essential | Applies the stored VM identity consumed by networking and diagnostics.                                                                              |
| A08 | Mask unusable Apple-vz SVE and update boot configuration                                      | Core-essential | Required compatibility repair for a supported VM platform.                                                                                          |
| A09 | Install Tailscale                                                                             | Core-essential | Tailscale SSH is the current core VM transport, not an optional user tool.                                                                          |
| A10 | Join the configured tailnet                                                                   | Core-essential | Makes the VM reachable through the core transport.                                                                                                  |
| A11 | Switch to and verify Tailscale SSH with retries                                               | Core-essential | Proves Phase B can run and determines whether provisioning succeeded.                                                                               |
| A12 | Invoke the platform's post-Tailscale-ready security hook and wait for reconnect               | Core-essential | Closes temporary platform access and re-establishes the transport after route changes.                                                              |
| A13 | Synchronize the operator's local SSH configuration                                            | Core-essential | Publishes the supported operator access path after connectivity is known.                                                                           |
| A14 | Record provisioning status/events and initialize the shared secret-redacting log              | Core-essential | Core owns truthful lifecycle state and diagnostics. Failure closes the log here; success keeps it open through B33.                                 |
| A15 | On bootstrap failure or interrupt, best-effort close the platform's provisioning access       | Core-essential | Fails closed for platforms with temporary bootstrap ingress while preserving the original error or interrupt.                                       |

A03's closed set is `openssh-server`, `curl`, `sudo`, `ca-certificates`, and `gnupg`
(`capabilities/vm_platform/cloud_init.py:12-20`). The sequence lives in
`capabilities/vm_platform/bootstrap_script.py:70-239` and
`vms/initializer/driver.py:70-239,303-501`. No Phase A step is plugin-bound.

### Phase B: repeatable VM and admin initialization

| ID  | Step in execution order                                                 | Classification                            | Rationale                                                                                     |
| --- | ----------------------------------------------------------------------- | ----------------------------------------- | --------------------------------------------------------------------------------------------- |
| B01 | Mark init in progress and load finalized apt/install-command registries | Core-essential orchestration              | Core owns lifecycle state and resolves resource views selected features consume.              |
| B02 | Reconcile SSH host-key preservation                                     | Core-essential                            | Repairs VMs created before A04 and preserves supported SSH identity.                          |
| B03 | Reconcile the Apple-vz SVE mask                                         | Core-essential                            | Repairs existing VMs affected by a supported-platform defect.                                 |
| B04 | Apply sysctl and `/proc` hardening                                      | Core-essential                            | Enforces the VM security baseline before later steps run.                                     |
| B05 | Diagnose VM DNS and surface the known broken-resolver latch             | Core-essential                            | Turns failures in core connectivity into actionable diagnosis.                                |
| B06 | Write the tailscaled cold-boot DNS ordering fix                         | Core-essential                            | Prevents a known core-connectivity failure on later boots.                                    |
| B07 | Write sshd `AcceptEnv` configuration                                    | Core-essential                            | Carries Agentworks runtime environment through supported SSH.                                 |
| B08 | Write sudo environment-preservation configuration                       | Core-essential                            | Preserves the core environment contract across privilege boundaries.                          |
| B09 | Write the console `sudo --preserve-env` authorization                   | Core-essential                            | Supports the core console's agent-pane transition.                                            |
| B10 | Install the closed core package set through an internal apt bootstrap   | Core-essential                            | Installs only binaries core invokes. It cannot consume operator apt resources.                |
| B11 | Reconcile selected apt sources                                          | Plugin-bound: `apt`                       | Consumes operator-selected apt resources and apt-specific key/list state.                     |
| B12 | Install direct and named apt packages                                   | Plugin-bound: `apt`                       | Consumes `vm-template.spec.apt` and `apt-package` resources through Debian apt.               |
| B13 | Write the VM-stable identity profile                                    | Core-essential                            | Publishes core VM, platform, and site identity to all users.                                  |
| B14 | Write `/etc/skel` shell seeds                                           | Core-essential                            | Keeps future managed users on the supported shell-source chain.                               |
| B15 | Install selected snap packages                                          | Plugin-bound: `snap`                      | Uses the independent snap daemon/store and per-package failure policy.                        |
| B16 | Run selected system install commands and collect PATH additions         | Plugin-bound: `install-command`           | Executes declarative arbitrary-shell installers with their test predicates.                   |
| B17 | Create tmux socket directories and remove stale sockets                 | Core-essential                            | Maintains filesystem state required by the core session implementation.                       |
| B18 | Set the admin login shell                                               | Core-essential                            | Converges the control identity's declared shell.                                              |
| B19 | Harden the admin home and verify its private primary group              | Core-essential                            | Enforces separation between managed users.                                                    |
| B20 | Reconcile admin authorized keys                                         | Core-essential                            | Preserves recoverable access to the control identity.                                         |
| B21 | Create the workspace parent, apply ACLs, then restore parent traversal  | Core-essential                            | Implements the multi-user workspace contract in its required order.                           |
| B22 | Write admin mise configuration                                          | Plugin-bound: `mise`                      | Consumes mise-specific tool/version and age-policy configuration.                             |
| B23 | Converge git `safe.directory`                                           | Core-essential                            | Supports the core shared-workspace ownership model.                                           |
| B24 | Materialize configured git credentials                                  | Core-essential                            | Core consumes credential-provider capabilities and writes standard git integration.           |
| B25 | Synchronize and install admin dotfiles                                  | Plugin-bound: `dotfiles`                  | Uses source-reference checkout/update behavior plus a checkout-local install command.         |
| B26 | Fetch the explicit admin mise lockfile                                  | Plugin-bound: `mise`                      | Consumes mise lockfile/source semantics after dotfiles and credentials settle.                |
| B27 | Run locked or unlocked admin mise install, then optionally prune        | Plugin-bound: `mise`                      | Executes mise's own convergence and retry policy.                                             |
| B28 | Run selected admin user install commands and collect PATH additions     | Plugin-bound: `install-command`           | Applies the declarative command mechanism at user scope.                                      |
| B29 | Write the admin Agentworks profile with identity and PATH additions     | Core-essential orchestration              | Core owns the shell-source file; enabled features contribute data rather than taking it over. |
| B30 | Write the admin Agentworks rc file                                      | Core orchestration with mise contribution | Core guarantees the rc file and source chain; mise owns its optional activation snippet.      |
| B31 | Run Claude marketplace and plugin setup for the admin                   | Plugin-bound: existing Claude integration | The roadmap assigns these fields to Claude's user-scope config and `user_init`.               |
| B32 | Re-ensure Agentworks profile and rc source lines                        | Core-essential                            | Repairs overwrite by user-owned setup while preserving core shell reachability.               |
| B33 | Record complete, partial, or failed status/events and close the log     | Core-essential                            | Core owns truthful repeatable-init state and warning aggregation.                             |

These calls are ordered in `vms/initializer/driver.py:504-838`. B10 retains `git`, `tmux`, `acl`,
and `zstd`, whose binaries core directly executes for workspace cloning, sessions, workspace ACLs,
and backup/restore. `tmuxinator` is not core-essential: Agentworks generates and links project
files, but never invokes its binary. Its installation becomes an apt-plugin resource that an
operator selects explicitly from a VM template when they want the optional integration. R1 does not
couple the workspace-template boolean to VM initialization. `unzip` and `jq` have no production
consumer and are removed from the unconditional list rather than assigned speculative owners.

### Agent-user bootstrap and self-configuration

| ID  | Step in execution order                                                           | Classification                            | Rationale                                                                               |
| --- | --------------------------------------------------------------------------------- | ----------------------------------------- | --------------------------------------------------------------------------------------- |
| C01 | Open the admin transport and derive the target home and shell                     | Core-essential orchestration              | Establishes the privileged half of agent realization.                                   |
| C02 | Probe the account; create it with a private group or converge its shell           | Core-essential                            | Creates the isolated Linux identity representing an Agentworks agent.                   |
| C03 | Harden the agent home to `0750`                                                   | Core-essential                            | Enforces isolation of agent credentials and state.                                      |
| C04 | Verify the private primary group and warn with repair guidance on drift           | Core-essential                            | Makes the home-mode invariant auditable for old or unusual images.                      |
| C05 | Ensure the tmux socket root and this agent's socket directory                     | Core-essential                            | Creates state required by core agent sessions.                                          |
| C06 | Remove stale sockets for this agent                                               | Core-essential                            | Converges the session filesystem before the agent connects.                             |
| C07 | Stage and atomically install the agent's authorized keys                          | Core-essential                            | Establishes direct agent-user SSH without later cross-user writes.                      |
| C08 | Open the agent-user transport                                                     | Core-essential orchestration              | Moves remaining mutations into the least-privileged identity.                           |
| C09 | Write the early identity-only Agentworks profile and source lines                 | Core-essential                            | Makes static identity available to later login-shell installers.                        |
| C10 | Write the Agentworks rc placeholder and source lines                              | Core orchestration with mise contribution | Core guarantees the file exists; mise owns its optional activation snippet.             |
| C11 | Converge git `safe.directory`                                                     | Core-essential                            | Mirrors the shared-workspace ownership contract for the agent.                          |
| C12 | Resolve, run up, and materialize git credentials                                  | Core-essential                            | Core consumes credential capabilities and writes standard git integration.              |
| C13 | Run selected user install commands                                                | Plugin-bound: `install-command`           | Executes declarative command resources under the agent identity.                        |
| C14 | Rewrite the Agentworks profile with command-contributed PATH additions            | Core-essential orchestration              | Preserves one core-owned profile while incorporating feature results.                   |
| C15 | Synchronize and install agent dotfiles                                            | Plugin-bound: `dotfiles`                  | Uses the dotfiles source/update/install mechanism under the agent identity.             |
| C16 | Add mise shims, write config, fetch lockfile, install tools, and optionally prune | Plugin-bound: `mise`                      | Executes the complete mise-specific agent convergence path.                             |
| C17 | Run Claude marketplace and plugin setup for the agent                             | Plugin-bound: existing Claude integration | Moves into Claude's agent/user-scope config and `user_init`, as settled by the roadmap. |
| C18 | Re-ensure Agentworks profile and rc source lines                                  | Core-essential                            | Repairs user-installer overwrites and preserves the core shell contract.                |

The sequence is `agents/initializer.py:78-420`. Agent deletion (`delete_agent_on_vm`) is lifecycle
cleanup rather than initialization or setup and stays core; it terminates processes, removes the
socket directory, and removes the Linux identity.

## Plugin-bound grouping

The shape test produces five new mechanisms. Apt, snap, mise, install commands, and dotfiles differ
in their external dependency, configuration family, and failure policy, so folding them together
would create a curated "developer tools" bundle rather than model reality. Claude setup follows the
separately settled harness-integration route into its existing plugin.

| Plugin            | Owned surfaces and execution                                                                                                                             | Shape-test rationale                                                                                                                                                                                                                                                                                                                         |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `apt`             | Direct apt lists; apt resource families and execution; installation of the mise binary for the `mise` plugin                                             | Depends on Debian apt, dpkg, curl, and GPG. Source and package failures are apt-specific (`vms/initializer/packages.py:25-200`). The closed core bootstrap stays internal. The mise plugin contributes its apt source/package declarations, while apt owns their execution.                                                                  |
| `snap`            | `vm-template.spec.snap` and per-package `snap install`                                                                                                   | Depends on the snap daemon and store, has no source-resource family, and fails independently per package (`vms/initializer/driver.py:646-656`).                                                                                                                                                                                              |
| `mise`            | Admin/agent `mise_*` fields; config and lockfile writes; tool installation; pruning; PATH and shell activation                                           | Depends on the mise CLI, source references, and its own formats and retry policy (`vms/initializer/mise.py`; `agents/initializer.py:644-774`). Its binary is an apt-installed prerequisite, so enabling mise requires apt. The HLA must define how that plugin dependency gives one exact remediation rather than duplicating apt mechanics. |
| `install-command` | System/user install-command resources; VM, admin, and agent runners; test predicates and PATH additions                                                  | Executes authored shell with test-file, test-directory, or executable idempotency and bounded timeouts (`vms/initializer/packages.py:203-288`; `agents/initializer.py:461-546`). System and user scopes share one mechanism.                                                                                                                 |
| `dotfiles`        | Admin/agent `dotfiles_*` fields; source parsing, directory synchronization, and the checkout-local install command                                       | Depends on source and git/file fetch semantics, then executes inside the checkout. Its config family and update behavior differ from generic install commands (`vms/initializer/driver.py:753-772`; `agents/initializer.py:312-383`).                                                                                                        |
| existing `claude` | Admin/agent `claude_marketplaces` and `claude_plugins`; CLI discovery, marketplace registration, and plugin installation through scoped integration init | This behavior belongs to the shipped Claude harness integration. Per the roadmap's settled contract, the fields move into its admin/user-scope config and execute through `user_init`, not through a new general installer contribution (`docs/sdd/2026-08-04-next-steps/target-state.md:142-162`).                                          |

## Config and resource ownership consequences

The plugin framework already publishes capability and manifest rows as present-but-disabled, and
apt/install-command references already enter the resource graph. Existing recipe gates prevent
disabled referenced rows from running. That covers named `apt-package`, `system-install-command`,
and `user-install-command` references, but not raw apt, snap, mise, or dotfiles fields. Claude's raw
fields follow the roadmap's separate scoped-integration config migration.

The HLA must choose a typed execution seat for apt, snap, mise, install-command, and dotfiles, plus
a declarative way for raw resource fields to state which plugin-owned mechanism they require. A
core-owned initializer capability is the leading option because it would use existing registration
and atomic seating. The HLA still needs to pressure-test it against declarable-resource forms and
the existing scope contracts. Arbitrary callbacks added directly to `Plugin` would create a second
registration system and are not the target architecture.

That design review also determines whether gating derivation genuinely consolidates and therefore
fires the capability descriptor contract's deferred `consumer_gating` trigger. R1 records the
trigger; it does not pre-commit the HLA's answer.

## R7 and C4 findings that the architecture must absorb

The current binary enablement pipeline is a useful base but is not the R7 contract yet:

- Only plugin opt-in can disable a row. There is no operator-authored universal resource disable
  list (`resources/graph.py:53-112`; `config/models.py:69-95`).
- Disabled-mark source and reason are discarded when the graph is built, so describe and doctor
  cannot report the actual decision (`resources/graph.py:150-177,420-449`).
- Resource-to-resource references to disabled rows commonly degrade readiness or fail only at use.
  R7 requires a finalize-time hard error. Settings references remain the explicit exception and keep
  presence-not-availability semantics (`config/references.py`).
- A disabled plugin manifest row is currently weak and silently yields to an operator row. The
  displaced row is discarded, conflicting with C4's explicit-disable-only replacement and its
  substitution provenance (`resources/registry.py:103-289`).

The HLA must make disable policy available when collisions are adjudicated, retain both the active
provider and any explicitly displaced provider in provenance, preserve disabled marks in the final
graph, and reject disabled resource edges during finalize. These are framework changes shared by all
resource kinds, not special behavior in installer plugins.

## Review decisions

R1 proposes the following decisions for review before the HLA and implementation plan are finalized:

1. Keep all Phase A steps in core.
2. Keep the Phase B and agent steps marked core-essential above, including the orchestration files
   that own order, lifecycle state, core profile/rc files, and feature contributions to them.
3. Keep only `git`, `tmux`, `acl`, and `zstd` in the internal Phase B apt bootstrap; make
   `tmuxinator` an apt-plugin resource and remove unconditional `unzip` and `jq`.
4. Create mechanism plugins named `apt`, `snap`, `mise`, `install-command`, and `dotfiles`.
5. Make mise depend on apt for its repository and binary instead of duplicating apt execution.
6. Move Claude marketplace/plugin setup through the roadmap-settled Claude integration's scoped
   config and `user_init` route.
7. Carry a typed initializer capability as the leading HLA option, while leaving the execution seat
   and `consumer_gating` decisions open until the HLA compares viable shapes.

No implementation move begins until this classification passes the phased artifact review required
by FRD R1.
