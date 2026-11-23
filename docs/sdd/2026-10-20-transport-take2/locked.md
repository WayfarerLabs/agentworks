# Transport take 2: locked

**Locked:** 2026-11-23

This effort is complete. Core reaches every managed VM through one platform-agnostic channel, the
installed OpenSSH client at the VM's tailnet address, with every request on the client's stdin.
Platforms keep lifecycle, provider networking, availability holds, one root channel for recovery
(`run_as_root`) and an optional native shell. The legacy transports are deleted. What stays open is
live evidence the operator ruled recorded here rather than closed (see
[Open live evidence](#open-live-evidence)). The effort superseded
[transport-improv](../2026-09-12-transport-improv/locked.md) and
[ssh-connection-contracts](../2026-09-05-ssh-connection-contracts/locked.md).

## What shipped

- [#856](https://github.com/WayfarerLabs/agentworks/pull/856): the HLA and plan, with the FRD's
  design-time rulings.
- PR A, [#863](https://github.com/WayfarerLabs/agentworks/pull/863) (slices 1 and 2): the per-VM
  host-key store with `agw vm import-host-keys`, and `agentworks.execution` with the operator exec
  surface.
- PR B, [#866](https://github.com/WayfarerLabs/agentworks/pull/866) (slices 3 and 4): first contact
  and the VM contract under vm-platform contract version 2, `run_as_root` and `native_shell` on
  every platform, `agw vm recover`, and `vm start` waiting for the VM and naming `vm recover` rather
  than rejoining Tailscale.
- PR C, [#876](https://github.com/WayfarerLabs/agentworks/pull/876) (slices 5 to 8): files and
  capability delivery, initialization, workspaces, sessions, consoles and backup on `VMAccess`, and
  the deletion of `agentworks.transports`, `agentworks.ssh` and every other legacy module.
- [#910](https://github.com/WayfarerLabs/agentworks/pull/910): the SSH config and Remote Lima upload
  follow-ups, which PR C merged and finished (plan, slice 8).
- [#916](https://github.com/WayfarerLabs/agentworks/pull/916): the generated workspace tmuxinator
  projects removed, with reinit retiring those earlier releases wrote.
- [#953](https://github.com/WayfarerLabs/agentworks/pull/953), in this closeout: `vm create` refuses
  a hostname with an underscore on every platform, moving the rule from WSL2 into core (operator
  ruling 2026-11-22).

## Where the design lives now

- `cli/agentworks/execution/README.md`: requests, outcomes, files, detached work, the terminal
  handover, root scripts and host-key trust.
- `cli/agentworks/capabilities/vm_platform/README.md`: the vm-platform contract for platform
  authors, and `cli/agentworks/capabilities/README.md` for the `admin_access()` and `agent_access()`
  accessors.
- ADR 0027 (one SSH execution channel), and ADR 0026 (environment travels inside the request), which
  supersedes ADRs 0014 and 0017. ADRs 0003, 0013, 0015, 0016, 0019, 0020 (closing SSH stdin) and
  0025 carry amendments.
- Operator guidance: `cli/command-reference.md`, `docs/guides/upgrading-to-0.20.md`, the guides
  under `docs/guides/` and the built-in guide topics.

No permanent artifact cites this directory.

## Evidence

[evidence-matrix.md](./evidence-matrix.md) is the record. Its historical status column holds 33
covered, 31 partial and 32 gap across 96 regular rows, plus two extras covered beyond their boxes (a
real OpenSSH 8.5 client on Linux, and managed SSH entries through `Include` on Windows).

At the operator's [2026-11-22 ruling](./frd.md#2026-11-22) one last tester pass ran at `main` (#948,
comment 7078499829, head `39df75ab2`, whose package is byte-identical to `main` at `2b1d69dac`),
with one cloud driven from Windows (GCP) and one from macOS (AWS), and Windows Server 2025 standing
in for Windows 11. It classified each row without replacing its status: 29 qualified subsets (the
three R11 rows among them), 10 current positives (one of them the underscore hostname failure), 49
not retested, 8 unavailable bed, GUI or console or conditionally unobserved, and the two extras kept
as history. Every protected gate passed and the pass found no runtime regression.

The pass also found that Python 3.13 rejects the PVE 8.4 CA while 3.12 verifies it (#951), that the
Windows bed's route to Proxmox needed pinned loopback forwards for short-name DNS (a bed
qualification), and that AWS ENI absence after teardown is unproved because
`ec2:DescribeNetworkInterfaces` was denied.

## Open live evidence

Under R11 missing coverage never counts as passing. Under the 2026-11-22 ruling, every row still
partial or gap after the closeout pass is recorded here as open live evidence. The groups are the
matrix's "Gaps after the closeout pass", with what would close each:

1. **Excluded experiments:** 389c, 389n, 389t; 415a to c; 423a to d; 427a, 427c; 432; 434d; 451;
   460a, 460b; delete of a cloud VM with no record; I18; N6, N8; I6a, S4; I48. The testing session
   ran under an operator stop direction excluding signal, forced interruption, outage,
   identity-mismatch, privilege-boundary and fault-injection experiments, which this effort's
   artifacts record only through the tester's report. Closes with: authorization for those
   experiments on the existing beds.
2. **Conditions not set up:** pre-python3 WSL2 recovery then reinit; I44; N5; N10; S9, S11. Closes
   with: those fixtures on existing beds (a WSL2 VM without python3, a transcript resume with a
   model call, a credential-bearing HTTPS clone, a DNS outage probe, a VM from an earlier release).
3. **Local Lima from Linux (216):** closes with a prepared Linux KVM bed.
4. **I21 round 2 Windows Terminal retest and I90 live:** close with an interactive Windows desktop
   and Windows Terminal lane.
5. **389f node removal and ephemeral key expiry, and I10's console view:** close with scoped
   Tailscale admin console access.
6. **Conditional cells:** 389s needs a WSL release that prints `WSL_E_DEFAULT_DISTRO_NOT_FOUND`;
   434c needs a supported fixture that advertises SVE.
7. **R11 pairings at end-state code:** Linux to local Lima is group 3. macOS to Azure and GCP, and
   Windows to AWS and Azure, fall outside the ruling's one cloud from each workstation, which the
   pass met.

Direct proof of AWS ENI absence needs ENI and quota read permissions. Out of scope by ruling or by
nature: WSL2 from Linux or macOS; an agent-served encrypted key on Windows, whose agent refuses from
a key-authenticated session; local Lima on Windows (2026-10-31, 2026-11-01); Debian 12 guests
(2026-11-05); a real OpenSSH 8.5 client on macOS and Windows (shim accepted, 2026-11-05).

## Superseded design text

The init LLD's `ADMIN_MANAGED` passage (`init-lld.md`, around lines 501 to 505) and the tmuxinator
mentions in `init-lld.md`, `sessions-lld.md`, `files-lld.md` and `hla.md` describe the workspace
tmuxinator projects that #916 removed; `main` has one `MANAGED` set for both accounts. The plan
records its other supersessions in place.

## Follow-up issues

Filed during the effort and open at the lock:

- #936: a console member server lost across `console stop` on WSL2 and Lima, unattributed (matrix
  row S2).
- #938: the migration-safety handshake test is flaky under load.
- #941: git credential timeout hardening.
- #946: `session create` and `agent exec` on a copied workspace fail on an unknown template.
- #947: the legacy SSH transport raised `UnicodeEncodeError` rather than `SSHError`, filed against a
  base before PR C, which deleted that transport.
- #950: Windows config loading reads the TOML file in the locale encoding (`config/load.py`).
- #951: Python 3.13's strict certificate checks reject the PVE 8.4 CA, which needs a durable
  compatibility decision.
