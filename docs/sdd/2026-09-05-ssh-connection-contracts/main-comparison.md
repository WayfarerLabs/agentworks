# SSH Design Comparison with the Production Baseline

- Inspected: 2026-09-16
- Production baseline: `e440a28c49935df722e4e80685ef12f6d8247ff8`
- Original design base: `7c744828`
- SSH design before this revision: `2494f6e2` (PR #796)
- Companion proposal: `6809827f64fb288880167fe2a4d9d7b42e29a21e` (PR #795)
- Reconciled: 2026-09-17 against merged transport baseline `857110df` (content `7228e3a2`)
- Scope: Source comparison and design revision; no runtime or joint-proof evidence

## Conclusion

The independent carrier and transport ownership split still fits current code. Keep the proposed
`agentworks.execution.carriers.ssh` boundary and the proof-first sequence. The material drift is in
execution consumers and the companion file contract, not a new production SSH implementation.

Source anchors below refer to the pinned production baseline. The merge of #795 changes no runtime
code relative to that snapshot. This document preserves comparison evidence, not a live transport
contract or complete caller inventory. Phase 2 retires it after transferring every remaining risk's
disposition to the plan or relevant LLD; transport refreshes its own inventory at integration.

## What is unchanged

`git diff 7c744828 e440a28c --` is empty for `cli/agentworks/ssh.py`, `cli/agentworks/transports/`,
`cli/agentworks/remote_exec.py`, `cli/agentworks/harness_setup/runner.py`,
`cli/agentworks/capabilities/vm_platform/lima.py` and `cli/agentworks/plugins/proxmox/transport.py`.

The old builders still select `StrictHostKeyChecking=accept-new` and ambient SSH configuration
(`ssh.py:336`, `transports/ssh.py:186`). Canonical guest connections still select
`operator.ssh_private_key` (`transports/__init__.py:94`), and remote Lima host access still accepts
an alias with no explicit user (`capabilities/vm_platform/lima.py:280`). The proposed isolated
connection/trust schema has not shipped (`config/models.py:27`). Manual aliases are maintained
separately by `ssh_config.py:76`; their existence does not make them execution inputs for the new
carrier.

The retirement set therefore remains valid. Reuse checks must cover more than runtime imports:
`native_files.py:18` still names the legacy Transport type and its methods use that contract.
`subprocess_io.py:81` also normalizes output newlines, so its decoder is unsuitable for the new
byte-preserving result boundary. These are source material, not shortcuts around independence.

## Changed consumers and design consequences

Paths in this table are under `cli/agentworks/`.

| Current evidence                                                                                                                                                               | Consequence and owner                                                                                                                                                                                                            |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `native_files.py:250` stages through the transport user and elevates guarded helper operations; `:334` and `:356` read/write through legacy command/copy methods.              | Transport migrates these consumers into shared file operations, preserving bytes, metadata, confinement and identity. SSH owns delivery only.                                                                                    |
| `artifacts/publication.py:71` publishes owned artifacts; `harness_setup/dispatch.py:209` checkpoints publication and retained ownership.                                       | Transport workflow acceptance covers partial publication, conflicting edits, failed checkpoints and retirement recovery, not merely a successful file transfer.                                                                  |
| `artifacts/native/probe.py:402` sends JSON stdin through a requested login/interactive shell with `tty=False`, then parses machine output at `:420`.                           | Shared preparation must preserve separate source/stdin, explicit shell behavior and output provenance. This is a current consumer for proof and later migration tests.                                                           |
| `sessions/manager/_lifecycle.py:898` stages replacement session artifacts before teardown; `artifacts/session.py:215` retains cleanup evidence.                                | Transport preserves lifecycle ordering and ownership through restart, restore and deletion. SSH must not replay an uncertain mutation or infer that local cleanup terminated guest work.                                         |
| `capabilities/harness_integration/activations.py:24` resolves activation maps with per-integration merging/removal.                                                            | Transport's fixtures and caller inventory must use current map-based configuration. Activation interpretation stays outside SSH.                                                                                                 |
| `capabilities/vm_platform/cloud_init.py:16` lists provisioning packages without Python; `:26` includes Python in initialization, installed at `vms/initializer/driver.py:551`. | Initial SSH/bootstrap and package installation cannot depend on Python already being installed. The shared helper design and proof inventory must distinguish provisioning, initialized guests, older guests and platform hosts. |

Existing behavior tests worth adapting include `cli/tests/artifacts/test_native_probe.py`,
`test_generated_sections.py`, `test_publication.py` and `test_session_lifecycle.py` in the same
directory, plus `cli/tests/test_native_file_boundaries.py` and
`cli/tests/test_system_package_prerequisites.py`. They are regression inputs, not
independent-carrier or live proof evidence.

## Companion design reconciliation

The original companion pin above records what the 2026-09-16 comparison inspected. The current
reference is now the merged [transport design](../2026-09-12-transport-improv/hla.md), its
[carrier contract](../2026-09-12-transport-improv/execution-contract.md#carrier-contract) and
[PoC definition](../2026-09-12-transport-improv/plan.md#2-prove-the-shared-boundary-before-broad-implementation).
Transport owns those definitions and acceptance criteria; SSH supplies implementation and
feasibility input. This revision removes the mirrored file-operation inventory and bilateral
contract-publication language identified in PR #796 feedback. Shared file semantics stay solely in
transport's artifacts, including its later file-only acceptance slice.

The operator directs two SSH implementation PRs: the complete SSH PoC in #796, then full
implementation under this same SDD. The [plan](plan.md) replaces the earlier separate-design landing
approach and records phase definitions of done. This artifact checkpoint has not passed the proof or
changed production callers. PR #795's artifacts remain with their owner.

## Open risks and required disposition

1. **Bootstrap tools.** Main's Python installation does not establish its availability before
   initialization or on a macOS platform host. Resolve supported tools and refusal behavior in the
   SSH LLD and shared preparation proof before accepting the carrier boundary. Preserve the current
   missing-Python refusal for dependent native setup (`native_files.py:241`) without making the
   package installer itself depend on that helper.
2. **Sensitive discovery output.** Current native discovery consumes a resolved environment
   (`harness_setup/inputs.py:67`, `plugins/claude/harness_integration.py:226`) and parses output
   (`artifacts/native/probe.py:420`). Transport R4 suppresses results that can reflect a sensitive
   environment. A mechanical caller conversion would lose required discovery data for such a
   binding. Transport must design and verify a discovery path compatible with that policy before
   cutover; any necessary requirement change returns to the operator. Do not relabel the environment
   as ordinary or add an SSH-specific output exemption. This is a migration risk inferred from code
   and the transport contract, not an observed new-stack failure.
3. **Trust maintenance.** Copying trust files preserves a snapshot. The SSH LLD must name the
   authority and update path for subsequent CA rotations and revocations, coordinated with writer
   ownership and rollback. No automatic synchronization is assumed.
4. **Unproven shared boundary.** Guest/client stderr separation, account-shell startup, source
   versus stdin delivery, cancellable borrowed streams and status-255 ambiguity remain proof
   obligations. Unchanged production code and a refreshed design do not settle them. The existing
   bounded proof charter gate remains open; this comparison uses no operator credentials, hosts or
   trust stores.

The [plan](plan.md) tracks these obligations within Phase 1 proof or Phase 2 implementation/cutover.
No architectural scope change is needed for the source comparison; unresolved behavior must be
settled at the named gate rather than silently weakened during implementation.
