# Transport take 2: locked

**Locked:** set when this file merges, which waits for #953, the excluded experiments and a soak
after the 0.20.0 release (operator, 2026-11-23).

The implementation is complete. Core reaches every managed VM through one platform-agnostic channel,
the installed OpenSSH client at the VM's tailnet address, with every request on the client's stdin.
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
  projects removed, with reinit retiring those earlier releases wrote;
  [#961](https://github.com/WayfarerLabs/agentworks/pull/961), which merges before this lock, says
  so in the built-in migration topic.
- [#953](https://github.com/WayfarerLabs/agentworks/pull/953), which merges before this lock:
  `vm create` refuses a hostname with an underscore on every platform, moving the rule from WSL2
  into core (operator ruling 2026-11-22).

## Where the design lives now

- `cli/agentworks/execution/README.md`: requests, outcomes, files, detached work, the terminal
  handover, root scripts and host-key trust.
- `cli/agentworks/capabilities/vm_platform/README.md`: the vm-platform contract for platform
  authors, and `cli/agentworks/capabilities/README.md` for the `admin_access()` and `agent_access()`
  accessors.
- ADR 0027 (one SSH execution channel), and ADR 0026 (environment travels inside the request),
  superseding ADRs 0014 and 0017. ADRs 0003, 0011, 0013, 0019, 0020 (closing SSH stdin) and 0025
  carry amendments for the mechanisms this design replaced, and ADRs 0015 and 0016 note where
  `SetEnv` was replaced.
- Operator guidance: `cli/command-reference.md`, `docs/guides/upgrading-to-0.20.md`, the guides
  under `docs/guides/` and the built-in guide topics.

No permanent artifact cites this directory.

## Evidence

[evidence-matrix.md](./evidence-matrix.md) is the record. Its introduction gives the historical
status tallies, including the correction to the figures given to the operator at the 2026-11-22
ruling.

At the operator's [2026-11-22 ruling](./frd.md#2026-11-22) one last tester pass ran at `main` (#948,
comment 7078499829, head `39df75ab2`, whose package is byte-identical to `main` at `2b1d69dac`),
with one cloud driven from Windows (GCP) and one from macOS (AWS), and Windows Server 2025 standing
in for Windows 11. It classified each row without replacing its status: 29 qualified subsets (the
three R11 rows among them), 10 current positives (one of them the underscore hostname failure), 49
not retested, 8 unavailable bed, GUI or console or conditionally unobserved, and the two extras kept
as history. Every protected gate passed and the pass found no runtime regression. The pass predates
`main`'s merges of PRs #916, #944 and #945, and #953, which merges before this lock; #916 had its
own live pass (PR comment 7078408324), and #953 has its own.

The pass also found that Python 3.13 rejects the PVE 8.4 CA while 3.12 verifies it (see
[Follow-up issues](#follow-up-issues)), that the Windows bed's route to Proxmox needed pinned
loopback forwards for short-name DNS (a bed qualification), and that AWS ENI absence after teardown
is unproved because `ec2:DescribeNetworkInterfaces` was denied.

## Open live evidence

Under R11 missing coverage never counts as passing. Under the 2026-11-22 ruling, every row whose
historical status is partial or gap is recorded here as open live evidence, whatever the closeout
pass added; the matrix's Closeout pass cells give each row's detail. The groups, with what would
close each:

1. **Excluded from the closeout pass:** 389c, 389n, 389t; 415a to c; 423a to d; 427a, 427c; 432;
   434d; 451; 460a, 460b; delete of a cloud VM with no record; I18; N6, N8, N10; I6a, S4; I48. The
   matrix says why they were excluded. The operator gave no such direction and ruled that these
   experiments run before the lock ([2026-11-23](./frd.md#2026-11-23)). Closes with: their results
   on the existing beds.

2. **Conditions not set up:** pre-python3 WSL2 recovery then reinit; I44; N5; S9, S11. Closes with:
   those fixtures on existing beds (a WSL2 VM without python3, a transcript resume with authorized
   model calls, a credential-bearing HTTPS clone, a VM from an earlier release).
3. **Local Lima from Linux (216, and the R11 Linux pairing row):** closes with a prepared Linux KVM
   bed.
4. **I21 round 2 Windows Terminal retest and I90 live:** close with an interactive Windows desktop
   and Windows Terminal lane.
5. **389f node removal and ephemeral key expiry, and I10's console view:** close with scoped
   Tailscale admin console access.
6. **Conditional cells:** 389s needs a WSL release that prints `WSL_E_DEFAULT_DISTRO_NOT_FOUND`;
   434c needs a supported fixture that advertises SVE.
7. **Moved in part, remainder open:**
   - 101: Azure and GCP from a non-Windows workstation; existing beds.
   - 389a: the bootstrap output size on WSL2 and Proxmox; existing beds.
   - 434a: the replaced key and the power cycle; macOS to local Lima.
   - 434f: the GCP startup marker, not read; any workstation to GCP.
   - Lima create on Apple VZ after an SVE restart: the restart never triggered; as 434c.
   - `vm shell --platform`: Proxmox's refusal; an existing PVE bed.
   - 519: Remote Lima from macOS, and a session start with model calls; existing beds with
     authorized model calls.
   - I47 on every harness, I40 and I38's conditions, F4 and F6 itemized on the V lane, and I35 to
     I53 itemized as live cells; existing beds.
   - N2: only uv's skip was observed, and the second reinit refreshed the `authorized_keys` mtime;
     existing beds.
   - N12: a forced idle shutdown during a WSL2 create; Windows to WSL2.
     - I8, S6 and S7 (disconnect and interrupt stages), S2 (a killed pane) and S8 (a clone failure):
       group 1's experiments.
     - 638: attach from every workstation to Remote Lima, whose Windows credential route was held,
       and backup interruption; that route, and group 1's experiments.

The rows the pass observed directly at `main`'s code (N1, N3, N4, N7, N9, N11, S1, S3 and S5) keep
their historical status, so they stay open too, with the fresh observation recorded in the matrix.
Direct proof of AWS ENI absence needs ENI and quota read permissions.

Out of scope by ruling or by nature: WSL2 from Linux or macOS; an agent-served encrypted key on
Windows, whose agent refuses from a key-authenticated session; local Lima on Windows (2026-10-31,
2026-11-01); Debian 12 guests (2026-11-05); a real OpenSSH 8.5 client on macOS and Windows (shim
accepted, 2026-11-05); the R11 pairings of macOS with Azure and GCP and of Windows with AWS and
Azure, beyond the one cloud from each workstation the 2026-11-22 ruling set, which the pass met.

## Superseded design text

The init LLD's `ADMIN_MANAGED` passage (`init-lld.md`, around lines 501 to 505) and the tmuxinator
mentions in `init-lld.md`, `sessions-lld.md`, `files-lld.md` and `hla.md` describe the workspace
tmuxinator projects that #916 removed; `main` has one `MANAGED` set for both accounts. The plan
records its other supersessions in place.

## Follow-up issues

Filed during the effort and open at the lock:

- #931: agent and workspace delete should refuse a misplaced session row before the confirmation
  prompt.
- #933: permanent artifacts cite SDD-internal identifiers from other efforts.
- #935: the SSH config's legacy managed-section removal drops everything after the first marker
  match.
- #936: a console member server lost across `console stop` on WSL2 and Lima, unattributed (matrix
  row S2).
- #937: the git credentials reconcile can remove the active generation when a link target ends in a
  newline.
- #938: the migration-safety handshake test is flaky under load.
- #940: SSH config sync crashes on a non-UTF-8 path in the managed file.
- #941: git credential timeout hardening.
- #946: `session create` and `agent exec` on a copied workspace fail on an unknown template.
- #950: Windows config loading reads the TOML file in the locale encoding (`config/load.py`).
- #951: Python 3.13's strict certificate checks reject the PVE 8.4 CA, which needs a durable
  compatibility decision.

Issue #947 (the legacy SSH transport raised `UnicodeEncodeError` rather than `SSHError`) was filed
against a base before PR C and closed at the lock as resolved by it: that transport is gone, and the
request envelope encodes every argument as UTF-8.
