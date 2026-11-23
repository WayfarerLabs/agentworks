# Transport take 2: evidence matrix

The closeout record of R11's live evidence, built from `main` at `2b1d69dac` (PR C merged) and the
tester reports on #856, #863, #866, #876, #910 and #916, and now also the closeout pass on #948.
Issue #923 has no comments. Line numbers are `plan.md` at `2b1d69dac` unless an LLD is named.

Status rules: **covered** means a report says the item ran against a real backend on the stated
cells at a head containing that code. **partial** means a subset of cells, a controlled stand-in
(loopback server, forwarder outage, instrumented seam, env-var fixture, guest `tailscale logout` in
place of the admin console), or an aggregate claim with no cell named. **gap** means no report ran
it at a qualifying head.

The Status column is history and stays as it is: 33 covered, 31 partial and 32 gap across the 96
regular rows (the three R11 rows count as covered), plus the two extras beyond the plan's boxes. The
**Closeout pass** column holds the #948 closeout report's fresh result for each row, in its own
classes, and never replaces a status or counts as a whole-row pass:

- **current positive:** the pass directly observed the item's condition at `main`'s code. For 770
  the observation is a failure.
- **qualified subset:** fresh evidence for part of the item; the cell names what stays open.
- **not retested:** the pass did not exercise the item; the cell says why.
- **unavailable bed**, **unavailable GUI**, **unavailable console:** the pass had no prepared Linux
  KVM bed, no interactive Windows desktop, or no Tailscale admin console.
- **conditional unobserved:** the item's precondition did not arise naturally, and forcing it was
  not permitted.
- **extra historical:** one of the two extras, kept as history with no new run.

The closeout report says the testing session ran under an operator stop direction that excluded
signal, forced interruption, outage, identity-mismatch, privilege-boundary and fault-injection
experiments. That direction went to the testing session, and this effort's artifacts hold no record
of it. The rows that need such an experiment read **not retested: excluded**. They stay gaps or
partials, which the 2026-11-22 ruling says are recorded in `locked.md`.

Every report is signed `agw-test-claude` or `agw-test-codex (agentworks integration-test session)`.
Windows beds were Windows Server 2022 (PR B rounds 1 and 2) and Windows Server 2025 (later). No
report ran Windows 11. Under the operator's [2026-11-22 ruling](./frd.md#2026-11-22), Server 2025
stands in for the Windows 11 cells.

## Heads cited

| PR / round    | Comment                                                   | Head                                       | Notes                                                                   |
| ------------- | --------------------------------------------------------- | ------------------------------------------ | ----------------------------------------------------------------------- |
| #863 r4       | 6571182125                                                | b8f3d4f3                                   | PR A ticks                                                              |
| #863 carried  | 6654000489                                                | 5bbe5d09a                                  | PR A carried cells                                                      |
| #863          | 6721879359                                                | aecdf67b                                   | real OpenSSH 8.5p1, Linux to Remote Lima                                |
| #866 r1       | 6656660451                                                | d49f6f94 (code of 85346499)                | full matrix, all six platforms                                          |
| #866 r2       | 6679977273                                                | f0c955679                                  | round 2 fixes; most slice 4 cells                                       |
| #866 r3       | 6753404865 (consolidated; lanes 6734599383 to 6751580508) | cf6fdeda                                   | contains plan round 3 fixes                                             |
| #866 r4       | 6769439296                                                | 50dab3ad                                   | contains plan round 5 fixes                                             |
| #866 r6       | 6782167606                                                | 29279561                                   | plan round 6                                                            |
| #866 r7       | 6783911580                                                | d3afcdc7                                   | plan round 7, live PVE                                                  |
| #866 r9       | 6788345855                                                | 695c36c3                                   | plan round 8                                                            |
| #866 r10, r11 | 6842977725, 6847606903                                    | 723f953f, f0ad3311                         |                                                                         |
| #866 r13      | 6887603387                                                | e2e1e14e (merged head)                     | Windows GUI shell checks                                                |
| #876 R1, R2   | 6993847810, 6996986110                                    | ce71f1f15, 00d2e1a8e                       | full matrix incl. PVE 8.4.21 and 9.2.26                                 |
| #876 R3       | 6999197387                                                | 5d2c6c85f                                  | macOS local Lima fresh create                                           |
| #876 R4, R5   | 7004537537, 7011070378                                    | cdf42d18f, 8f198f3bc                       | last Azure lifecycle (R5)                                               |
| #876 R6 to R9 | 7014521865, 7027341433, 7057919064, 7058787764            | 68d13ada3, 087df109b, 434c3624f, 81e8b844a | GCP, AWS SG only, Remote Lima, WSL2                                     |
| #876 R10      | 7067082289                                                | 334a45823 (merged head)                    | Linux to Remote Lima workspaces and harness; Windows config sync, no VM |
| #948 closeout | 7078499829                                                | 39df75ab2 (protected main 2b1d69dac)       | closeout pass; no package change, wheel byte-identical to main          |

PRs #910 and #916 have heads that contain PR B but not PR C (bases `1e26eda66`, `ccb0e08c5`,
`2a7f6d1a7`), so they can credit slices 1 to 4 only; they add Remote Lima, WSL2 and GCP creates but
no new item.

## Slice 1: Trust (PR A)

| Item                                                           | Status | Evidence                                                                                                                                  | Note                                                                                                                                                                                                                                     | Closeout pass                                                                     |
| -------------------------------------------------------------- | ------ | ----------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Encrypted key served by the agent on the remaining cells (101) | gap    | Covered cells only: Linux to Remote Lima (#863 6571182125, b8f3d4f3); Linux to PVE 9 and macOS to local Lima (#863 6654000489, 5bbe5d09a) | Missing: macOS to Remote Lima, and AWS, Azure, GCP from any non-Windows workstation. #863 6721879359 ran an 8.5 agent case against a loopback sshd only (stand-in). Windows impossible (agent refuses from a key-authenticated session). | qualified subset: macOS to AWS and Remote Lima (same Mac host); Linux clouds open |

## Slice 2: Execution core (PR A)

| Item                                                                               | Status                | Evidence                                                                                      | Note                                                                                                                                                                | Closeout pass                                             |
| ---------------------------------------------------------------------------------- | --------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| Local Lima from Linux (216)                                                        | gap                   | Every report marks it impossible on the bed                                                   | Linux bed has no `/dev/kvm`. #916 7033159047 calls it an inventory gap, not intrinsic.                                                                              | unavailable bed: no prepared Linux KVM bed                |
| I21 round 2 Windows Terminal retest (execution-lld I21: B2, B4, B5, #893 F1 repro) | partial               | #866 6887603387, e2e1e14e, Windows Server 2025 GUI to Remote Lima                             | `vm shell` and `vm shell --platform` kept all twelve scrollback markers, Find worked. B2, B4, B5 and the `session attach` Ctrl-B d repro not named. Not Windows 11. | unavailable GUI: no interactive desktop; native PTYs only |
| I90 live (NUL-led `ssh -t` on a Windows console; execution-lld slice 8)            | partial               | same as I21 (e2e1e14e contains `601e81d87`)                                                   | Also a redirected exec kept 38 exact bytes with no extra NUL. Rides on I21's cells.                                                                                 | unavailable GUI: as I21                                   |
| OpenSSH 8.5 (not open; shim accepted 2026-11-05)                                   | covered beyond ruling | #863 6721879359 aecdf67b Linux to Remote Lima; #866 r3 cf6fdeda Linux to PVE, AWS, Azure, GCP | Real 8.5p1 on Linux only.                                                                                                                                           | extra historical: no new 8.5 run                          |

## Slice 3: Contract (PR B)

| Item                                                           | Status  | Evidence                                                                                                                                                                                         | Note                                                                   | Closeout pass                                                      |
| -------------------------------------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------- | ------------------------------------------------------------------ |
| Create and reinit on every platform (296)                      | covered | #866 6656660451 d49f6f94 (all six); #866 r3 cf6fdeda; #876 R1/R2 (Linux to AWS, Azure, GCP, PVE 8, PVE 9, Remote Lima), R3 5d2c6c85f (macOS to local Lima), R4/R5 (Windows to Remote Lima, WSL2) | Contract runs in every create.                                         | qualified subset: fresh create and reinit on ten pairings, not all |
| I25 live half (contract-lld): hold spans Phase A and B on WSL2 | covered | #866 6656660451 d49f6f94 and r3 cf6fdeda, Windows to WSL2                                                                                                                                        | Tracer removing the hold killed Phase B; real hold kept the distro up. | qualified subset: ordinary WSL2 create; no hold-removal control    |

## Slice 4: Recovery and platform surface (PR B)

Main box (389):

| Item                                                                                          | Status  | Evidence                                                                        | Note                                                                                                                           | Closeout pass                                                 |
| --------------------------------------------------------------------------------------------- | ------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------- |
| a. Create on every platform, bootstrap output size on WSL2 and Proxmox                        | partial | #866 6656660451, 6753404865; #876 R1/R2                                         | Creates covered; output size never recorded (only script size 8,346 bytes).                                                    | qualified subset: creates only; output size still unmeasured  |
| b. Proxmox recover of a stopped VM in one run                                                 | covered | #866 6656660451 d49f6f94, 6679977273 f0c955679; Linux to PVE 8, 9               | Aggregate statement.                                                                                                           | not retested: no stopped-VM recover on PVE                    |
| c. Proxmox create whose first-boot upgrade restarts the guest agent                           | partial | #866 6656660451, 6679977273, 6736314225 (cf6fdeda); #876 R1/R2                  | r3 lists the real first-boot case as still open; PR C induced a QGA restart in the cloud-init wait (stand-in for the upgrade). | not retested: excluded                                        |
| d. Guest agent ping on PVE 8 and 9                                                            | covered | #866 6736314225 cf6fdeda, 6783911580 d3afcdc7                                   | Stopped QGA: refusal, zero dispatches, both majors.                                                                            | not retested: excluded                                        |
| e. Rekey, delete's logout, recover on every platform                                          | covered | #866 6656660451 (all six), 6753404865, 6782167606, 6842977725, 6847606903       | Recover re-run at PR C on clouds, PVE, Remote Lima, WSL2; rekey and logout not re-run at PR C.                                 | qualified subset: healthy recover, logout on delete; no rekey |
| f. `vm start` after node removal and after ephemeral key expiry                               | partial | #866 6656660451, 6679977273                                                     | Guest `tailscale logout` stood in for admin-console removal; ephemeral expiry never run.                                       | unavailable console                                           |
| g. Broken sshd recovered on WSL2, Proxmox, Lima                                               | covered | #866 6783911580 (PVE 8.4, 9.2), 6842977725 (WSL2, Remote Lima)                  | Local Lima only in r1's aggregate.                                                                                             | not retested: excluded                                        |
| h. W6 restart over sshd on Lima, Remote Lima, a cloud                                         | covered | #866 6656660451, 6679977273                                                     | Root V lane passes where it applies; W6 not named per cell.                                                                    | not retested: excluded                                        |
| i. W7 on WSL2 with setsid                                                                     | covered | #866 6679977273 f0c955679, 6753404865                                           | Failed r1, fixed r2.                                                                                                           | not retested: excluded                                        |
| j. Recover after key change, then `vm exec`                                                   | covered | #866 6679977273, 6736314225                                                     |                                                                                                                                | not retested: excluded                                        |
| k. Rekey with dead public sshd on a cloud                                                     | covered | #866 6679977273                                                                 | One wait, names recover.                                                                                                       | not retested: excluded                                        |
| l. Closed terminal during cloud create; Ctrl-Break on Windows                                 | covered | #866 6679977273 f0c955679; Linux to AWS, Azure, GCP; Windows Server 2022 to AWS | Pipe form is 415a.                                                                                                             | not retested: excluded                                        |
| m. Windows decoys for tailscale and PowerShell                                                | covered | #866 6679977273, Windows Server 2022                                            |                                                                                                                                | not retested: excluded                                        |
| n. I18: failed backend delete keeps row and store (PVE 8, 9; Remote Lima)                     | partial | #866 6679977273                                                                 | Locked VM and unreachable host real; API down via forwarder outage. Violated r1, fixed r2.                                     | not retested: excluded                                        |
| o. Failed rollback keeps row (PVE, Lima, WSL2); `delete --force` clears a killed create's row | covered | #866 6679977273                                                                 |                                                                                                                                | not retested: excluded                                        |
| p. Closed terminal during create on Remote Lima and Proxmox                                   | covered | #866 6679977273                                                                 | Terminal form only.                                                                                                            | not retested: excluded                                        |
| q. Recover of a removed, not purged python3                                                   | covered | #866 6679977273                                                                 | Failed r1, fixed r2.                                                                                                           | not retested: excluded                                        |
| r. cloud-init status 2 on AWS and Azure                                                       | covered | #866 6679977273                                                                 | Induced, plus one natural Azure hit.                                                                                           | not retested: excluded                                        |
| s. WSL2 empty `wsl --list` on a release printing the token                                    | gap     | #866 6679977273, 6842977725                                                     | No such release at hand (box says "if one is at hand").                                                                        | conditional unobserved: installed WSL lists empty, exit 0     |
| t. Lost store record on one cloud and Remote Lima                                             | partial | #866 6842977725, 6847606903 (Remote Lima)                                       | Cloud half only an aggregate line, no cell named.                                                                              | not retested: excluded                                        |
| u. V cells of W1, W3 to W9                                                                    | covered | #866 6656660451, 6679977273                                                     |                                                                                                                                | not retested: excluded                                        |

Follow-up boxes:

| Item                                                                                                          | Status  | Evidence                                                                                        | Note                                                                                         | Closeout pass                                                                          |
| ------------------------------------------------------------------------------------------------------------- | ------- | ----------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| 415a. Create and rollback with a lost output reader, each platform, Linux and Windows                         | gap     | #866 6679977273 measured it failing before round 3's fix                                        | Not re-run at cf6fdeda or later.                                                             | not retested: excluded                                                                 |
| 415b. Clouds: interrupt as Phase A writes COMPLETE; termination during Ctrl-C rollback                        | partial | #866 6769439296 50dab3ad; #876 6996986110                                                       | Self-SIGINT on an unattached SG and NSG, no VM, no GCP; PR C one Azure allow-create SIGINT.  | not retested: excluded                                                                 |
| 415c. WSL2 Ctrl-C during base install; `CREATE_NO_WINDOW`                                                     | gap     | #866 6679977273 (pre-fix)                                                                       | Not re-run after round 3.                                                                    | not retested: excluded                                                                 |
| 423a to d. Proxmox identity checks (gone VM, reissued VMID, lost `next_id` race, unauditable VMID)            | gap     | #866 6679977273 found failures; r3 6753404865 says only "power/identity refusals"               | No case named at a head with the fix.                                                        | not retested: excluded                                                                 |
| 427a. Unbindable site refused, then `--force` with identifiers warned                                         | gap     | #866 6679977273 (pre-ruling)                                                                    |                                                                                              | not retested: excluded                                                                 |
| 427b. Logout before the provider delete                                                                       | covered | #866 6842977725, 6847606903; Linux to Remote Lima, Windows to WSL2 and Remote Lima              | No PVE or cloud cell.                                                                        | qualified subset: logout on public delete, clouds, WSL2, PVE, Lima; order not measured |
| 427c. Delete failing after the logout, hint wording                                                           | gap     | none                                                                                            |                                                                                              | not retested: excluded                                                                 |
| 432. Windows decoys for op, git, code, taskkill, ssh, scp                                                     | gap     | #866 6679977273 ran op and git pre-fix                                                          | code, taskkill never run; no re-run after the fix.                                           | not retested: excluded                                                                 |
| 434a. Local Lima from macOS keeps `.bashrc` and source lines across stop/start and recover; replaced key gone | partial | #866 6769439296 50dab3ad                                                                        | `.bashrc` kept over two stop/start; source lines across recover and the key half not stated. | qualified subset: reinit and healthy recover; no key or power-cycle case               |
| 434b. OS hostname and node name after Lima stop/start                                                         | covered | #866 6769439296                                                                                 |                                                                                              | qualified subset: stored `p948m_local`, guest `p948mlocal`; no stop/start              |
| 434c. Apple-vz SVE restart path                                                                               | gap     | #866 6769439296 says unmeasured                                                                 |                                                                                              | conditional unobserved: VZ advertised no SVE                                           |
| 434d. Remote Lima wrapper kill under macOS `/bin/bash`                                                        | gap     | none                                                                                            |                                                                                              | not retested: excluded                                                                 |
| 434e. WSL2 keeps `vms.hostname`, `wsl.conf` newlines                                                          | covered | #866 6769439296, Windows Server 2025                                                            |                                                                                              | qualified subset: ordinary WSL2 lifecycle; newlines not read separately                |
| 434f. GCP stop/start finds the startup marker                                                                 | partial | #876 R1, R2, R5 (stop/start passed)                                                             | Marker not reported; #866 r4 ran no GCP lifecycle.                                           | qualified subset: Windows to GCP create; marker not read                               |
| 434g. No `limactl` with `AW_SECRET_` env                                                                      | covered | #866 6769439296                                                                                 |                                                                                              | not retested: excluded                                                                 |
| 434h. Windows Python 3.15 resolves cffi 2.1.1 cp315                                                           | covered | #866 6769439296                                                                                 | Full frozen install still fails on PyYAML source build.                                      | not retested: Windows ran Python 3.13 and 3.12                                         |
| 451. Proxmox behind a delaying proxy: recover's deadline                                                      | partial | #866 6782167606 29279561, 6783911580 d3afcdc7                                                   | Loopback HTTP server plus instrumented handoff; live PVE behind a proxy not run.             | not retested: excluded                                                                 |
| 460a. Rekey refused before dispatch repeats the full command                                                  | partial | #866 6783911580 (real PVE at round 7 head), 6788345855 695c36c3 (round 8 via instrumented seam) | Round 8 hint never seen on a real outage.                                                    | not retested: excluded                                                                 |
| 460b. Guest agent near the deadline sends nothing past it                                                     | partial | same                                                                                            | Instrumented delay on real guests.                                                           | not retested: excluded                                                                 |

Platform-lld live cells not in the plan box:

| Item                                                       | Status  | Evidence                                | Note                                                        | Closeout pass                                                    |
| ---------------------------------------------------------- | ------- | --------------------------------------- | ----------------------------------------------------------- | ---------------------------------------------------------------- |
| Admin console shows the key used once (I10)                | partial | #866 6656660451                         | No console; join-once and zero key hits stand in.           | unavailable console                                              |
| Lima create on Apple VZ: join first time after SVE restart | partial | #866 6744196600, 6769439296             | Joined first time; whether the restart ran is not recorded. | qualified subset: VZ join with no SVE; restart not triggered     |
| Delete of a cloud VM with no record                        | gap     | none                                    |                                                             | not retested: excluded                                           |
| Pre-python3 WSL2 VM recovered, then reinitialized          | partial | #866 6656660451, 6679977273             | Reinit afterwards not stated.                               | not retested: no pre-python3 VM                                  |
| `vm shell --platform` everywhere, and Proxmox's refusal    | partial | #866 6656660451, 6753404865, 6887603387 | Proxmox refusal not named; WSL2 native PTY not run.         | qualified subset: WSL2 native PTY, Linux PTY; no Proxmox refusal |

Incident rows (platform-lld, contract-lld):

| Row      | Status  | Evidence                                                   | Closeout pass                             |
| -------- | ------- | ---------------------------------------------------------- | ----------------------------------------- |
| I10      | covered | #866 6656660451 (0 key hits; console view absent)          | unavailable console: key use not readable |
| I17      | covered | #866 6746643536 cf6fdeda, Linux to Azure                   | not retested: excluded                    |
| I18      | partial | as 389n                                                    | not retested: excluded                    |
| I24      | covered | #866 6656660451, PVE and WSL2                              | not retested: excluded                    |
| I29      | covered | #866 6656660451, 6753404865                                | not retested: excluded                    |
| I30 (W4) | covered | #866 6656660451, Lima and Azure                            | not retested: excluded                    |
| I34 (W7) | covered | #866 6679977273; original SIGTTIN symptom never reproduced | not retested: excluded                    |

## Slice 5: Files and capability delivery (PR C)

| Item                                                                                                    | Status  | Evidence                                                                | Note                                                                                                   | Closeout pass                                                                               |
| ------------------------------------------------------------------------------------------------------- | ------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| Harness setup and session start with artifacts, Remote Lima from macOS and one cloud from Windows (519) | partial | #876 R1, R2, R5, R10 (Linux to Remote Lima and clouds; Windows to WSL2) | Neither stated cell ran. Claude and Codex used a loopback provider fixture; only Grok used a real API. | qualified subset: macOS to AWS, Windows to GCP native MCP config accepted; zero model calls |
| Second no-op setup (I47)                                                                                | gap     | none                                                                    |                                                                                                        | qualified subset: repeat reinit on macOS to AWS and Linux; not every harness                |
| `/etc/claude-code` elevated artifacts (I40); generated section over an ACL (I38)                        | partial | #876 R3, R4                                                             | ACL kept through `Files.write_if` only.                                                                | qualified subset: artifact delivery and ACLs; I40, I38 conditions not run                   |
| Session start resuming from a transcript (I44)                                                          | gap     | #876 R2 names it a gap                                                  |                                                                                                        | not retested: no transcript resume or model call                                            |
| `gh` readiness with and without login (I48)                                                             | gap     | none                                                                    |                                                                                                        | not retested: authentication experiments excluded                                           |
| F4 and F6 on every platform's V lane                                                                    | gap     | none                                                                    | Only L and S lanes reported.                                                                           | qualified subset: hints read by real users; vectors not itemized                            |
| I35 to I53 "each row also gets a live cell" (files-lld)                                                 | gap     | none                                                                    | No report itemizes these rows beyond the checks above.                                                 | qualified subset: artifact families, MCP clients; rows not itemized                         |

## Slice 6: Initialization (PR C)

| Item                                                                | Status  | Evidence                                                                                                                           | Note                                                                                                                                   | Closeout pass                                                                 |
| ------------------------------------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Create and reinit on every platform (565)                           | covered | #876 R1, R2 (Linux to AWS, Azure, GCP, PVE 8, PVE 9, Remote Lima), R3 (macOS to local Lima), R4, R5 (Windows to Remote Lima, WSL2) | Agent create and reinit ran. Template git credential not shown; dotfiles and mise lockfile shown (R1, R2, R7, R8). PVE only at R1, R2. | qualified subset: fresh create and reinit on ten pairings, not all            |
| N1. 0750 homes, private groups, `hidepid=1` (I62, I65, I67)         | gap     | none                                                                                                                               |                                                                                                                                        | current positive: held Remote Lima and Azure, metadata only                   |
| N2. Quiet second reinit, installed commands skipped (I61, I69)      | gap     | none                                                                                                                               |                                                                                                                                        | qualified subset: second reinit skipped uv; `authorized_keys` mtime refreshed |
| N3. cloud-init drop-in after reinit (I9)                            | gap     | none                                                                                                                               |                                                                                                                                        | current positive: Azure reinit kept the drop-in                               |
| N4. One `safe.directory` (I71)                                      | gap     | none                                                                                                                               |                                                                                                                                        | current positive: Azure, one `safe.directory`                                 |
| N5. `sh` agent git credential HTTPS clone (I72)                     | gap     | none                                                                                                                               |                                                                                                                                        | not retested: no credential-bearing clone                                     |
| N6. `agent exec` after reinit restores `authorized_keys` (I19, I64) | gap     | none                                                                                                                               |                                                                                                                                        | not retested: excluded                                                        |
| N7. `.ssh` 0700, `authorized_keys` 0600 (I79)                       | gap     | none                                                                                                                               |                                                                                                                                        | current positive: 0700 and 0600 measured                                      |
| N8. Reinit after `.bashrc` linked into dotfiles (I59, decision 3)   | gap     | none                                                                                                                               | Only a hard-link case (R1 finding, fixed R2).                                                                                          | not retested: excluded                                                        |
| N9. Agent delete leaves no socket dir (I74)                         | gap     | none                                                                                                                               |                                                                                                                                        | current positive: socket dir absent after agent delete                        |
| N10. DNS check before apt (I73)                                     | gap     | none                                                                                                                               |                                                                                                                                        | not retested: no DNS outage probe                                             |
| N11. SVE gate no-op on macOS local Lima (I76)                       | gap     | none                                                                                                                               | macOS local Lima ran; gate not reported.                                                                                               | current positive: Apple VZ gate no-op                                         |
| N12. WSL2 create holds through idle shutdown (I25, init-lld)        | gap     | #876 R4 observed idle only                                                                                                         |                                                                                                                                        | qualified subset: healthy create; no forced idle shutdown                     |

## Slice 7: Workspaces, sessions and backup (PR C)

| Item                                                                                                      | Status  | Evidence                                                            | Note                                                                                                                                       | Closeout pass                                                                  |
| --------------------------------------------------------------------------------------------------------- | ------- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------ |
| I6a (621)                                                                                                 | partial | #876 R1, R2, R3 (Windows to Remote Lima, WSL2; macOS to local Lima) | Missing pane and window restored; killed attach `ssh` and killed attach pane not run.                                                      | not retested: excluded                                                         |
| I8 (621)                                                                                                  | partial | #876 R2 00d2e1a8e, cell not named                                   | Real outage during compression only; download stage and WSL2 not run.                                                                      | qualified subset: ordinary WSL2 backup; no interruption                        |
| Session and console on Remote Lima from three workstations and WSL2; backup on WSL2 and Remote Lima (638) | partial | see rows                                                            | PTY attach covered from Windows and macOS to Remote Lima and on WSL2; Linux to Remote Lima attach not stated; backup not on WSL2.          | qualified subset: Linux PTY, add-shell, WSL2 backup; no all-workstation attach |
| S1. Session lifecycle, newline secret (I21, I85, decision 3)                                              | partial | #876 R1, R2, R4, R5                                                 | Newline value shown only in exec env.                                                                                                      | current positive: newline value in Shell and add-shell, Remote Lima            |
| S2. Console lifecycle, restore, add-shell with agent secrets (I6a, I6, I19)                               | partial | #876 R2 to R9                                                       | add-shell and killed pane not run. Console stop lost the member server at R7, R9 (Remote Lima) and R8 (WSL2), unattributed (matches #936). | qualified subset: add-shell with agent secret; no killed pane                  |
| S3. Agent pane interactive with `TERM` (T2)                                                               | gap     | none                                                                |                                                                                                                                            | current positive: agent PTY, `TERM=xterm-256color`                             |
| S4. Kill console attach `ssh`, re-attach (I6a)                                                            | gap     | none                                                                |                                                                                                                                            | not retested: excluded                                                         |
| S5. `session list` with a stopped VM within the bound (I88)                                               | gap     | none                                                                |                                                                                                                                            | current positive: natural WSL2 stop listed, 1.961 s                            |
| S6. Backup forced disconnect, archive and download (I8, I81, I25)                                         | partial | #876 R2                                                             | Compression stage only, cell not named.                                                                                                    | qualified subset: ordinary WSL2 backup; no disconnect                          |
| S7. Backup interrupted, archive and download (I82, I20)                                                   | partial | #876 R1, R2                                                         | Archive Ctrl-C clean; transfer SIGINT incidental; WSL2 not stated.                                                                         | qualified subset: ordinary WSL2 backup; no interrupt                           |
| S8. Workspace clone, copy between VMs, rehome, repair (I67, I87)                                          | partial | #876 R7 to R10                                                      | Clone failure (I87) and copy between two VMs not reported.                                                                                 | qualified subset: clone, copies, rehome, repair; no clone failure              |
| S9. Reinit removes setenv and `env_keep`; sudo printenv records (deletion 6)                              | partial | #876 R1, R2, upgraded v0.19.0 VM                                    | printenv records missing; platform not named.                                                                                              | not retested: no upgraded VM                                                   |
| S10. AcceptEnv removed; SetEnv stops reaching a shell (D3)                                                | covered | #876 R2 00d2e1a8e                                                   | Platform not named.                                                                                                                        | not retested: excluded                                                         |
| S11. Earlier-release VM loses `tmux-agent-access`, socket modes reset (D4)                                | partial | #876 R1, R2                                                         | Group and modes not reported.                                                                                                              | not retested: no earlier-release VM                                            |

## Slice 8 and optional items (PR C)

| Item                                                                        | Status                    | Evidence                                                        | Note                                                                                                                                                                                                 | Closeout pass                                                             |
| --------------------------------------------------------------------------- | ------------------------- | --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| Underscore hostname on a non-WSL2 platform (770, platform-lld slice 8 cell) | partial                   | #876 R4 7004537537 cdf42d18f, macOS to local Lima, leased guest | Incidental: DB hostname has an underscore, guest OS hostname normalized. No fresh create, `hostnamectl`, node name or managed SSH entry check. If confirmed, the plan says the rule moves into core. | current positive (a failure): see [Closeout findings](#closeout-findings) |
| Managed SSH entries through Include on Windows                              | covered (not an open box) | #876 R9 7058787764                                              | Apostrophe profile only via a `USERPROFILE` fixture.                                                                                                                                                 | extra historical: no fresh Include vector                                 |

## R11 pairings at the end-state code (PR C heads)

R11 asks for every platform and workstation pairing. Best evidence at a PR C head, with the PR B
evidence where PR C has none:

| Workstation                | Remote Lima | local Lima                    | WSL2       | PVE 8                                         | PVE 9                                                  | AWS                | Azure                      | GCP                        |
| -------------------------- | ----------- | ----------------------------- | ---------- | --------------------------------------------- | ------------------------------------------------------ | ------------------ | -------------------------- | -------------------------- |
| Linux                      | covered     | gap (no `/dev/kvm`)           | impossible | covered (R1, R2)                              | covered (R1, R2)                                       | covered            | covered (last R5)          | covered                    |
| macOS                      | covered     | covered                       | impossible | gap (PR B: covered at r2; V lanes only at r1) | gap (PR B: covered at r2; not run at r1, cert forward) | gap (PR B covered) | gap (PR B: "out of scope") | gap (PR B: "out of scope") |
| Windows (Server 2022/2025) | covered     | not applicable (no `limactl`) | covered    | gap (PR B covered)                            | gap (PR B: covered at r2, no V lane)                   | gap (PR B covered) | gap (PR B: "out of scope") | gap (PR B: "out of scope") |

The "out of scope" labels come from the tester's charter at #866 r1; no operator disposition for
those pairings appears in the FRD. The 2026-11-04 ruling carried "a cloud driven from Windows"
forward to closeout.

The PR B Proxmox parentheticals rest on #866 r2 (6679977273, f0c955679), whose coverage table marks
PVE 8.4.21 and 9.2.11 covered from macOS and from Windows, Windows on 9.2 except a truly gone guest;
its V lane ran 23 of 23 on macOS to PVE 8 and 9 and Windows to PVE 8, not Windows to PVE 9. At r1
(6656660451) macOS reached PVE 8 on the root and exec V lanes only, and neither reached PVE 9.

The closeout pass (#948 7078499829 at `39df75ab2`, whose package is byte-identical to `main` at
`2b1d69dac`) adds fresh pairings at the end-state code:

| Workstation         | Remote Lima                                 | local Lima       | WSL2            | PVE 8.4         | PVE 9.2         | AWS                          | Azure           | GCP             |
| ------------------- | ------------------------------------------- | ---------------- | --------------- | --------------- | --------------- | ---------------------------- | --------------- | --------------- |
| Linux               | held VM operations only, no fresh lifecycle | unavailable bed  | impossible      | not rerun       | not rerun       | not selected                 | fresh lifecycle | not selected    |
| macOS               | fresh lifecycle (same Mac over real SSH)    | fresh (Apple VZ) | impossible      | fresh lifecycle | fresh lifecycle | fresh lifecycle, power cycle | not selected    | not selected    |
| Windows Server 2025 | held (management-key delegation denied)     | not applicable   | fresh lifecycle | fresh lifecycle | fresh lifecycle | not selected                 | not selected    | fresh lifecycle |

A fresh lifecycle is public create, initialization and reinit, the reached user, workspace, artifact
and Shell operations, and public teardown with native or provider verification. Remote Lima from
macOS exercises the remoting path but adds no physical host. Both PVE majors ran from both
workstations with genuine CA verification. Stock Python 3.13.18 rejected the PVE 8.4 CA (#951) and
accepted the 9.2 one; Python 3.12.16 runtimes verified both and completed the lifecycles. Windows to
PVE ran through pinned loopback forwards (see [Closeout findings](#closeout-findings)). macOS to AWS
and Windows to GCP are the ruling's one cloud from each. The three R11 rows count as qualified
subsets in the closeout classes.

## Closeout findings

From the #948 closeout pass (7078499829, `39df75ab2`):

- **Underscore hostname off WSL2 (770).** macOS to local Lima: create, reinit and recover returned 0
  while the DB kept `p948m_local` and the guest's `hostnamectl`, `uname` and `/etc/hostname` read
  `p948mlocal`; the mismatch persisted across reinit and recover. Linux to Azure: `p948l_under`
  passed schema validation, then the provider refused it (`InvalidParameter`, exit 1) and rollback
  removed the partial resources cleanly; a supported name then created. This meets the plan's
  condition for moving the refusal into core, and the operator ruled a small PR before the lock that
  does so ([FRD, 2026-11-22](./frd.md#2026-11-22)).
- **PVE 8.4 CA under Python 3.13 (#951).** Stock Python 3.13.18 rejected the PVE 8.4 CA with
  `CA cert does not include key usage extension`; Python 3.12.16 verified it and completed the PVE
  8.4 lifecycles. TLS verification was not weakened.
- **Windows short-name DNS (bed qualification).** On the Windows bed's route to PVE, resolving by
  short name failed (`getaddrinfo failed`, exit 1) before any VM was allocated; pinned loopback
  forwards resolved it. This qualifies the bed, not the product.
- **AWS ENI absence unproved.** After teardown AWS reported the instance terminated with no attached
  interfaces or block devices, and its disk and security group were absent, but
  `ec2:DescribeNetworkInterfaces` was IAM-denied, so direct ENI absence is not proved.

## Gaps at the 2026-11-22 ruling

Grouped by the bed that would close them. The operator ruled one last tester pass at `main` over
every cell the existing beds reach, with one cloud driven from Windows and one from macOS; what it
cannot reach is recorded as a gap in `locked.md`.

1. **macOS workstation to Proxmox (PVE 8 and 9, with the certificate forward) and to AWS, Azure,
   GCP:** R11 pairings at PR C code; encrypted key via agent on clouds (101).
2. **macOS workstation to Remote Lima:** encrypted key via agent (101); harness setup and session
   start with artifacts (519, I44, I47, I48, I40, I38); Remote Lima wrapper kill under `/bin/bash`
   (434d).
3. **Local Lima on macOS (Apple silicon, VZ):** SVE restart path (434c) and recording that it ran on
   create; SVE gate no-op (N11); `.bashrc` and source lines across recover, replaced key gone (434a
   remainder).
4. **Windows workstation to a cloud (AWS, Azure or GCP) and to Proxmox:** harness setup with
   artifacts from Windows to one cloud (519); R11 Windows pairings at PR C code; lost-reader create
   and rollback on Windows (415a).
5. **Windows workstation (Server 2025 stands in, 2026-11-22):** decoys for op, git, code, taskkill,
   ssh, scp (432).
6. **Windows to WSL2:** Ctrl-C during the base install and the `CREATE_NO_WINDOW` measure (415c);
   create held through idle shutdown at PR C (N12); backup forced disconnect and interrupt on WSL2
   (S6, S7, I8); pre-python3 WSL2 recover then reinit; WSL2 native shell; the
   `WSL_E_DEFAULT_DISTRO_NOT_FOUND` listing (389s, conditional on a release that prints it).
7. **Windows Terminal (Server 2025 stands in for Windows 11, 2026-11-22):** I21 round 2 retest (B2,
   B4, B5, `session attach` F1 repro) and I90 live.
8. **Proxmox PVE 8 and 9 from Linux:** identity checks (423a to d); live proxy-delayed recover
   (451); round 8 rekey hint on a real outage and near-deadline agent (460a, 460b); a real
   first-boot upgrade restarting the guest agent (389c); a real API-down delete with `pveproxy`
   restarted (389n, I18); unbindable-site delete and post-logout failure (427a, 427c);
   `vm shell --platform` refusal.
9. **Clouds from Linux (AWS, Azure, GCP):** interrupt at COMPLETE and termination during rollback
   with a real VM, GCP included (415b); lost-reader create (415a); GCP startup marker (434f); delete
   of a cloud VM with no record; lost store record on one cloud (389t); encrypted key via agent
   (101, Linux works); `vm delete` ruling cells on a cloud (427a, 427c). Azure lifecycle last ran at
   R5 (8f198f3bc), before rounds 5 to 9 touched route code.
10. **Tailscale admin console access:** node removal then `vm start` (389f); ephemeral key expiry
    (389f); key used once in the console (I10 cell).
11. **Any one cell at PR C code (Linux to Remote Lima suffices for most):** init rows N1 to N10; S3,
    S4, S5; newline session secret (S1); console add-shell and killed attach pane (S2, I6a); backup
    download-stage disconnect and interrupt on Remote Lima (S6, S7, I8); workspace clone failure and
    copy between two VMs (S8); printenv records (S9); D4 group and modes (S11); F4 and F6 V-lane
    vectors; underscore hostname create on Remote Lima or a cloud with `hostnamectl`, node name,
    reinit and managed entries (770).
12. **Linux workstation with KVM:** local Lima from Linux (216; R11 pairing).
13. **Impossible or out of scope (for disposition, not a bed):** WSL2 from Linux or macOS
    (workstation-bound); encrypted key via agent on Windows (agent refuses); local Lima on Windows
    (no `limactl`); Debian 12 cells (dropped 2026-11-05); real OpenSSH 8.5 on macOS and Windows
    (shim accepted; Linux ran a real 8.5p1).

## Gaps after the closeout pass

Rows still partial or gap that the closeout pass did not move, by reason. Rows it moved in part
(qualified subset) keep their open remainder in the Closeout pass cell.

1. **Excluded by the operator's direction:** 389c, 389n, 389t; 415a, 415b, 415c; 423a to d; 427a,
   427c; 432; 434d; 451; 460a, 460b; delete of a cloud VM with no record; I18; N6, N8; I6a, S4; I48
   (authentication experiments).
2. **Condition not set up in the pass:** pre-python3 WSL2 VM recovered, then reinitialized; I44 (no
   transcript resume or model call); N5 (no credential-bearing HTTPS clone); N10 (no DNS outage
   probe); S9 and S11 (no upgraded or earlier-release VM).
3. **Unavailable bed:** local Lima from Linux (216; no prepared Linux KVM bed).
4. **Unavailable GUI:** I21 round 2 Windows Terminal retest; I90 live.
5. **Unavailable console:** 389f node removal and ephemeral key expiry; I10's key used once in the
   console.
6. **Conditional, unobserved:** 389s, since the installed WSL release lists empty with exit 0; 434c,
   since Apple VZ advertised no SVE.
7. **R11 pairings not run at end-state code:** Linux to local Lima (item 3); macOS to Azure and GCP;
   Windows to AWS and Azure. The ruling's one cloud from each workstation is met.

## Could not determine

- Which cloud, if any, ran the lost-store recovery (389t).
- Whether #866 r3's "power/identity refusals" includes any 423 case.
- Whether the SVE mask restart ran in the macOS creates.
- Which platform the v0.19.0 upgrade VM was (S9, S10, S11) and which cell ran the backup outage and
  interrupt (S6, S7, I8).
- Whether "managed files" in #876 R1 included VM-scope artifacts.
- Which platform #863 6566207981's loaded-key create ran on (Linux, probably Remote Lima).
