# Transport Improvements: Migration Outline

- Status: First draft; sequencing will be refined after FRD/HLA review
- Baseline: `7c744828184ccb0ad9ffd90a8a02226384fb384e`, inspected 2026-09-12

## Inventory and destination

The current delivery implementations are SSH, Lima, remote Lima, WSL2, and Proxmox QGA. AWS, Azure,
and GCP reuse SSH for native access. The public abstraction has two tiers; `RunContext` currently
delivers the richer tier only.

| Current owner                                                  | Target change                                                                                   |
| -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `transports/base.py`, concrete transports, Proxmox transport   | Common execution target above carrier delivery; explicit optional interaction.                  |
| `transports/__init__.py`, VM platform native/provision results | Construct the new target under the same explicit route ownership.                               |
| `capabilities/base.py`, VM/agent/session context constructors  | Deliver the new target type through existing identity accessors.                                |
| Capability readiness and operations, setup invocation runners  | Consume the common type without rebuilding transports or context authority.                     |
| `harness_setup/runner.py`                                      | Move prepared environment policy into shared target defaults; retire forwarding implementation. |
| `remote_exec.py`, backup, initialization, native logout        | Use managed job start/observe/wait/dispose with explicit retention.                             |
| VM/agent exec and shell, sessions/consoles                     | Preserve command/stdin and terminal behavior while using shared execution and feature checks.   |
| SSH-named shared result/error/logger types                     | Move generic execution facts into transport-neutral vocabulary.                                 |

## Sequence

1. Review required behavior and optional features through the FRD/HLA draft PR.
2. Resolve the execution/context and file/job LLDs, feasibility evidence, and complete caller
   inventory. Reconcile the accepted contract against then-current SSH work and PR #789.
3. Build common preparation and carrier conformance. Move each existing adapter and its factory
   together; prove the required native contract without optional features.
4. Migrate `RunContext` producers and consumers, direct service callers, setup execution, files, and
   detached workflows. Remove obsolete APIs and copied wrappers within the same complete migration
   increment rather than publishing a permanent compatibility bridge.
5. Validate live, update permanent collateral with the behavior change, and close the effort only
   after the old entry points and assumptions are retired.

This is an ordering outline, not an approved PR stack. Implementation may use several commits in one
branch. A future split must produce independently complete increments with an explicit removal point
for any temporary bridge. The current PR contains design artifacts only.

## Worked example: native recovery command

The current PR #789 proposal routes native exec through a separate manager function calling the
limited `run` contract. In the destination, the operation boundary selects native access and minimal
recovery environment, opens the platform route/hold, and creates a context carrying a native admin
target. The common execution service interprets the command, dispatches it, and renders the same
result/error contract as ordinary execution. An optional shell request can be refused independently.

If the repair needs a script, file, or detached process, the same target supplies it. Nothing at the
caller switches on Proxmox, assumes SCP, or embeds a `nohup` sequence. The route can close after
acknowledged detached launch; later observation opens a fresh authorized context for its job.

## Risks and safeguards

- Existing shell strings may depend on expansion or login profiles. Classify every caller before
  moving it to literal arguments or scripts; do not mechanically split shell source into argv.
- Environment and elevation changes can alter guest authority or expose secrets. Preserve scoped
  resolution and prove whole-operation identity and secret absence across adapters.
- Old detached artifacts can describe running work. Inventory their actual retention and consumers;
  do not reinterpret arbitrary legacy paths as new job references or delete unowned work. The job
  LLD must choose a bounded drain/adoption strategy before retiring the old reader.
- A context target can outlive its route accidentally. Lifetime checks and later-observation tests
  must cover both normal exit and exceptions.
- In-flight SSH changes can move migration sites. Re-inventory after contract review; compare
  semantics to the accepted design before deciding what to reuse.
- Contract versions and any job persistence changes require an explicit compatibility decision after
  the caller inventory. This draft does not assume that aliases or a database migration are
  necessary.
