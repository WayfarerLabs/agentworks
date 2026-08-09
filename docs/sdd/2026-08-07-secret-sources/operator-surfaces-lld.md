# LLD: Secret Operator Surfaces and Consumer Migration

<!-- cspell:ignore isatty ljust onepassword proxmox repr unresolvable -->

- Status: Reviewed and implemented; operator contract correction in progress
- Scope: Phase 6 operator-surface contract, consumed by implementation Phases 7 and 8
- Governing artifacts: [FRD](./frd.md), [HLA](./hla.md),
  [migration strategy](./migration-strategy.md), [source contract](./source-contract-lld.md), and
  [resolution lifecycle](./resolution-lifecycle-lld.md)
- Code baseline: detached Phase 6 worktree at `2287812c`

## Purpose and current-state correction

This document fixes the last consumer, error, inspection, verification, renderer, export,
completion, and documentation decisions before the temporary runtime adapters are removed. It does
not redesign source identity, backend selection, mapping validation, source clients, timeout
ownership, outcome legality, or value containment. Those contracts are fixed by the two earlier
LLDs.

The branch baseline includes mainline onboarding work that did not exist at the migration strategy's
dated snapshot: `agw secret verify NAME`, `--allow-interactive`, a one-name `SecretVerification`
proof record, and guide teaching already exist. They currently route through
`resolve_secrets_quiet`, discard a successful value dictionary, and translate only the first failed
outcome to the older error taxonomy. The implementation MUST migrate this surface. It MUST NOT add a
second command, keep the single-name service beside the final batch service, or assume Phase 8
starts from no command.

The final planned syntax remains authoritative:

```text
agw secret verify NAME... [--allow-interaction]
```

The pre-release `--allow-interactive` spelling is removed rather than retained as an alias. Phase 8
is the command-shape cutover. Phase 7 first moves the existing command's service path to typed,
value-free outcomes so Phase 7 can truthfully delete every dict-returning compatibility resolver.

## Fixed decisions

The following are implementation inputs:

- Runtime names are source names. No consumer may restore direct backend lookup or the
  `ActiveBackend` vocabulary.
- `ResolutionOutcome` is the one diagnostic result model. Verification renderers, future JSON
  renderers, partial inspection failures, and operation error formatting project this record. They
  do not define parallel category or result models.
- `ResolutionBatch` remains the only typed object that can contain resolved values. It is internal
  to the secrets runtime and never reaches a renderer, output handler, generic serializer, error,
  doctor record, describe record, or verification service result.
- Ordinary CLI operations derive one `InteractionPolicy` at their top-level command composition
  boundary: `ALLOW` only when stdin is a TTY and global `--non-interactive` is absent, otherwise
  `REFUSE`. They inject that immutable policy through managers and services; nothing below the
  command boundary rereads ambient interaction state. Verification is separate: its CLI defaults to
  `REFUSE`; `--allow-interaction` selects `ALLOW` unless global `--non-interactive` rejects the
  flag. Direct service callers always supply an exact policy and are not governed by CLI ambient
  state.
- `describe`, `list`, and `doctor` do no reads. Their preview is pure mapping applicability plus
  folded readiness. In particular, they do not read an environment variable, open a client, run
  `op`, authenticate, prompt, or call `resolve_batch`.
- `agw env show --resolve` remains the sole partial value-reveal inspection path. Its values and its
  value-free failures travel in separate objects.
- A complete operation still has one boundary resolution, one operation-lifetime cache, first
  registration wins, late registration errors, gate no-double-resolution, and per-node scoped
  delivery.
- Verification renders all typed outcomes and exits 1 if any unique requested name is unresolved. A
  resolution outcome is data for this explicit proof surface, not an exception. Registry,
  configuration, and command-usage failures still raise their ordinary typed errors before a result
  table is emitted.

## Baseline inventory

### Runtime and operator shapes at `2287812c`

| Concern          | Current code                                                                                    | Final owner                                                                                              |
| ---------------- | ----------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Active chain     | `secrets/resolve.py` defines `ActiveSource`, plus `ActiveBackend` and `active_backends` aliases | `ActiveSource` and `active_sources`, internal to `agentworks.secrets.resolve`                            |
| Typed core       | `resolve_batch` returns private-value `ResolutionBatch` and value-free outcomes                 | Retained; final safe projections below replace wrappers                                                  |
| Complete wrapper | `_resolve_complete_for_legacy_callers` and `resolve_secrets(errors=None)` return dictionaries   | Delete; `Resolver` and final high-level operation services consume `ResolutionBatch.complete_or_raise()` |
| Partial wrapper  | `resolve_secrets(errors=dict)` converts outcomes to strings                                     | Delete; `env show` receives values separately from `ResolutionOutcome` records                           |
| Verify wrapper   | `resolve_secrets_quiet` returns a dictionary or maps only the first outcome to an error         | Delete in Phase 7; existing verify service returns all outcomes                                          |
| Preview          | `preview_resolution` returns `str \| None` and opens ready non-interactive clients              | Replace with a pure, typed `ResolutionPreview`                                                           |
| Operation cache  | `Resolver.resolve` calls old wrappers and stores `dict[str, str]`                               | It calls the typed batch once, completes or raises, then owns the operation dictionary                   |
| Gate reads       | activation and session scope call old wrappers directly                                         | Resolver-owned gate and authorized late-repair methods                                                   |
| Describe/list    | backend-named records call active aliases and preview                                           | Source-named, provenance-bearing, pure records                                                           |
| Doctor           | backend readiness plus a probing per-secret preview                                             | Backend readiness stays; add source status and make secret preview non-probing                           |
| Verify command   | one `name`, `--allow-interactive`, one generic success line                                     | variadic `names`, `--allow-interaction`, one typed row per unique name                                   |
| Completions      | dynamic secret completer is bound to `("secret.verify", "name")`                                | bind it to variadic `names` and prove every shell repeats it                                             |

### Complete production consumer ledger

Every production reference at the baseline has this disposition:

| File                          | Baseline symbol/use                                             | Phase 7 destination                                                             |
| ----------------------------- | --------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `secrets/resolve.py`          | `ActiveBackend = ActiveSource`                                  | Delete alias                                                                    |
| `secrets/resolve.py`          | `active_backends` list adapter                                  | Delete; internal callers use tuple-returning `active_sources`                   |
| `secrets/resolve.py`          | `_compatibility_error`                                          | Delete; `outcomes.complete_resolution_error` owns the final map                 |
| `secrets/resolve.py`          | `_inspection_projection`                                        | Replace with the fenced partial-reveal projection                               |
| `secrets/resolve.py`          | `_resolve_complete_for_legacy_callers`                          | Delete                                                                          |
| `secrets/resolve.py`          | `resolve_secrets` and `errors` out-parameter                    | Delete                                                                          |
| `secrets/resolve.py`          | `resolve_secrets_quiet` and `_verification_compatibility_error` | Delete                                                                          |
| `secrets/resolve.py`          | unused `disabled_plugin_backends`                               | Delete; Registry source rows own this visibility                                |
| `secrets/resolve.py`          | probing `preview_resolution`                                    | Move pure typed projection to `secrets/preview.py`                              |
| `secrets/resolver.py`         | operation boundary wrappers                                     | Call `active_sources`, `resolve_batch`, and final outcome error projection      |
| `secrets/orchestration.py`    | standalone `resolve_for_command` wrapper call                   | Call the typed core directly with explicit operation policy                     |
| `orchestration/activation.py` | singleton gate wrapper call                                     | Delegate to `Resolver.resolve_gate`                                             |
| `sessions/manager/_scope.py`  | authorized late repair wrapper call                             | Delegate to `Resolver.resolve_late_repair` after its existing declaration check |
| `secrets/verification.py`     | aliases, quiet dictionary, `SecretVerification`                 | Return `tuple[ResolutionOutcome, ...]`; remove the parallel proof record        |
| `secrets/inspect.py`          | active alias, backend vocabulary, probing preview               | Source records plus pure typed preview                                          |
| `doctor.py`                   | active alias and probing preview                                | Source status builder plus pure typed preview                                   |
| `orchestration/secrets.py`    | `ActiveBackend` annotation and string/none predictions          | `ActiveSource` internally and typed operation preview records                   |
| `env/show.py`                 | active alias, wrapper, `errors: dict[str, str]`                 | `PartialResolution` with separate values and outcomes                           |
| `secrets/__init__.py`         | exports aliases and wrapper                                     | Remove retired exports and add only the final exports listed below              |
| `secrets/base.py`             | docstring names `resolve_secrets`                               | Reword to the typed operation boundary                                          |

There are no production imports from `agentworks.secrets.backends`, `agentworks.secrets.env_var`, or
`agentworks.secrets.prompt`, and no production secrets-package import of `SECRET_BACKEND_REGISTRY`.
The negative relocation test in `tests/capabilities/test_secret_backend_relocation.py` remains.
Phase 7 adds the exact semantic retired-seam guard specified below.

Explicit policy also changes composition sites even when they do not import a retired symbol. Phase
7 updates every current `Resolver(config, registry)` construction in:

- `sessions/manager/_lifecycle.py`, `sessions/manager/_scope.py`, and
  `sessions/manager/_create_build.py`;
- `workspaces/manager/create.py`;
- `vms/manager/exec.py`, `vms/manager/lifecycle.py`, `vms/manager/power.py`, and both constructions
  in `vms/manager/boundary.py`;
- `agents/manager/lifecycle.py`.

It also updates every standalone `resolve_for_command` call in `sessions/manager/_lifecycle.py`,
`sessions/multi_console/{crud,attach,restore}.py`, and `vms/manager/power.py`'s conditional
`start_vm` rejoin path. `vms/manager/tailscale.py` deletes its standalone resolution branch.
Policies originate at the top-level CLI or other caller and are forwarded unchanged through these
manager call graphs; none of these manager sites derives or rereads the policy.

### Recursive interaction-propagation inventory

The baseline was traced recursively outward from `Resolver`, `resolve_for_command`, the partial
reveal projection, `gated_vm_boundary`, `_live_vm_boundary`, `_batch_vm_boundary`, and
`_prepare_vm`. A function is in the graph when any branch can reach one of those seams; an empty
target, no-status, missing-VM, nested-teardown, or best-effort branch does not remove it. The final
Phase 7 manifests below are exhaustive, not examples.

Every Python service entry in this manifest adds required keyword-only, no-default
`interaction: InteractionPolicy`. Its first executable statement is exactly:

```python
interaction = validate_interaction_policy(interaction)
```

That statement precedes local imports, config or registry construction, DB reads, output, prompts,
mutation, `try`, `with`, and closure construction. The validated local is the only object forwarded
on every downstream edge. Internal boundaries that accept policy obey the same first-statement rule.
A nested consumer validates again at its own boundary but receives the identical enum object.

| Graph slice            | Complete public Python service manifest                                                                                                                                                                                          | Downstream secret-bearing edge                                                                                                                                     |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Core/proof             | `secrets.resolver.Resolver.__init__`; `secrets.orchestration.resolve_for_command`; `env.show.show_env`; `secrets.verification.verify_secrets`                                                                                    | typed batch; standalone complete projection; partial reveal; verification discard                                                                                  |
| VM lifecycle           | `vms.manager.lifecycle.create_vm`; `vms.manager.lifecycle.reinit_vm`; `vms.manager.power.rekey_vm`                                                                                                                               | operation `Resolver`                                                                                                                                               |
| VM live/power          | `vms.manager.power.describe_vm`; `vms.manager.power.start_vm`; `vms.manager.power.stop_vm`; `vms.manager.power.delete_vm`                                                                                                        | `_live_vm_boundary`; start conditionally uses `resolve_for_command`, then `_ensure_tailscale` with an explicit resolved auth source                                |
| VM gated/access        | `vms.backup.backup_vm`; `vms.manager.exec.shell_vm`; `vms.manager.exec.exec_vm`; `vms.manager.tailscale.port_forward_vm`                                                                                                         | `gated_vm_boundary`; an auto-start reaches `LiveVMNode.auto_start`, which passes its existing gate reader to `_ensure_tailscale`                                   |
| VM credential          | `vms.manager.exec.add_git_credential`                                                                                                                                                                                            | operation `Resolver`                                                                                                                                               |
| Workspace              | `workspaces.manager.create.create_workspace`; `workspaces.manager.repair.repair_workspace`; `workspaces.manager.rehome.rehome_workspace`; `workspaces.manager.copy.copy_workspace`; `workspaces.manager.delete.delete_workspace` | `Resolver`, `_rehome_vm`, or one/two `gated_vm_boundary` calls; delete's nested-node branch forwards without constructing a second gate                            |
| Agent lifecycle/access | `agents.manager.lifecycle.create_agent`; `agents.manager.lifecycle.reinit_agent`; `agents.manager.lifecycle.delete_agent`; `agents.manager.access.shell_agent`; `agents.manager.access.exec_agent`                               | `Resolver` or `gated_vm_boundary`; delete's nested-node branch forwards without a second gate                                                                      |
| Agent grants           | `agents.grants.grant_workspaces`; `agents.grants.revoke_workspaces`                                                                                                                                                              | `gated_vm_boundary`                                                                                                                                                |
| Session create/resume  | `sessions.manager._create.create_session`; `sessions.manager._lifecycle.resume_session`; `sessions.manager._lifecycle.resume_all_sessions`                                                                                       | `_build_session_graph`/`Resolver`; resume uses `Resolver` plus `resolve_for_command`; batch resume uses `_batch_vm_boundary` and recursively calls singular resume |
| Session singular       | `sessions.manager._lifecycle.stop_session`; `sessions.manager._queries.delete_session`; `sessions.manager._queries.describe_session`; `sessions.manager._queries.attach_session`; `sessions.manager._logs.session_logs`          | `_prepare_vm` then `gated_vm_boundary`; delete also reaches policy-bearing workspace/agent cleanup closures                                                        |
| Session batch          | `sessions.manager._queries.list_sessions`; `sessions.manager._lifecycle.stop_all_sessions`                                                                                                                                       | `_batch_vm_boundary`; list remains in the graph even when `no_status=True` bypasses that branch                                                                    |
| Console                | `sessions.multi_console.attach.attach_console`; `sessions.multi_console.restore.restore_session`; `sessions.multi_console.crud.add_sessions`; `sessions.multi_console.crud.add_shell`                                            | attach/restore use `_prepare_vm_target_for_attach` then `gated_vm_boundary` and can use `resolve_for_command`; add-sessions/add-shell use `resolve_for_command`    |

The matching ordinary CLI composition-root manifest is:

| CLI module               | Complete secret-consuming command roots                                                                                                                                 |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cli.commands.env`       | `env_show`                                                                                                                                                              |
| `cli.commands.vm`        | `vm_create`, `vm_backup`, `vm_describe`, `vm_start`, `vm_stop`, `vm_delete`, `vm_rekey`, `vm_reinit`, `vm_exec`, `vm_shell`, `vm_port_forward`, `vm_add_git_credential` |
| `cli.commands.workspace` | `workspace_create`, `workspace_rehome`, `workspace_repair`, `workspace_delete`, `workspace_copy`                                                                        |
| `cli.commands.agent`     | `agent_create`, `agent_reinit`, `agent_grant_workspaces`, `agent_revoke_workspaces`, `agent_exec`, `agent_shell`, `agent_delete`                                        |
| `cli.commands.session`   | `session_create`, `session_describe`, `session_list`, `session_stop`, `session_resume`, `session_attach`, `session_delete`, `session_logs`                              |
| `cli.commands.console`   | `console_attach`, `console_add_sessions`, `console_add_shell`, `console_restore_session`                                                                                |

These Typer roots do not expose a framework enum as a CLI option. Instead, after module-level
imports make the two policy helpers available, the first executable statement of each ordinary root
is exactly:

```python
interaction = validate_interaction_policy(ordinary_interaction_policy())
```

It then passes `interaction=interaction` to every selected service branch. `session_stop` forwards
to both singular and batch services; `session_resume` forwards to `_resume_sessions`, which
validates first and forwards to singular or batch resume. The verification command stays in its
separate manifest: `cli.commands.secret.secret_verify` selects its explicit default/flag policy
before config work, validates it, and passes it to `verify_secrets`.

The following baseline public commands are deliberately outside the manifest because recursive
inspection finds no path to a secret consumer or named boundary: VM list, verify-connection, and
logs; workspace list and describe; agent list and describe; console create, list, describe, delete,
remove-sessions, and reorder-sessions; secret list and describe. Their DB, static transport, or pure
inspection behavior does not acquire a policy merely because a neighboring command does.

The internal edge manifest is equally explicit:

```text
Resolver.__init__ -> stored exact interaction
Resolver.resolve -> ResolutionPolicy -> resolve_batch
Resolver.resolve_gate -> ResolutionPolicy -> resolve_batch
Resolver.resolve_late_repair -> ResolutionPolicy -> resolve_batch
resolve_for_command -> ResolutionPolicy -> resolve_batch
resolve_partial_for_reveal -> ResolutionPolicy -> resolve_batch
verify_secrets -> ResolutionPolicy -> resolve_batch
gate_secret_resolver callback -> Resolver.resolve_gate
_scope._gate_resolver callback -> Resolver.resolve_late_repair
preflight_all -> require_predicted_refs
require_predicted_refs -> predict_resolution
predict_resolution -> preview_operation_resolution
show_env -> resolve_partial_for_reveal
create_session -> _build_session_graph -> Resolver
create_session -> _preflight_and_resolve -> preflight_all
_build_session_graph -> pending_workspace_node -> PendingWorkspaceNode.__init__
_build_session_graph -> pending_agent_node -> PendingAgentNode.__init__
create-session unwind -> PendingWorkspaceNode.teardown -> delete_workspace
create-session unwind -> PendingAgentNode.teardown -> delete_agent
resume_session -> Resolver
resume_session -> resolve_for_command
resume_all_sessions -> _batch_vm_boundary
resume_all_sessions -> resume_session
stop_session -> _prepare_vm -> gated_vm_boundary
delete_session -> _prepare_vm -> gated_vm_boundary
delete_session -> _cleanup_now_empty_workspace -> delete_workspace
delete_session -> _cleanup_now_empty_agent -> delete_agent
describe_session -> _prepare_vm -> gated_vm_boundary
attach_session -> _prepare_vm -> gated_vm_boundary
session_logs -> _prepare_vm -> gated_vm_boundary
list_sessions -> _batch_vm_boundary -> Resolver
stop_all_sessions -> _batch_vm_boundary -> Resolver
attach_console -> _prepare_vm_target_for_attach -> gated_vm_boundary
attach_console -> resolve_for_command
restore_session -> _prepare_vm_target_for_attach -> gated_vm_boundary
restore_session -> resolve_for_command
add_sessions -> resolve_for_command
add_shell -> resolve_for_command
create_workspace -> Resolver
create_workspace -> pending_workspace_node -> PendingWorkspaceNode.__init__
repair_workspace -> gated_vm_boundary
rehome_workspace -> _rehome_vm -> gated_vm_boundary
copy_workspace -> gated_vm_boundary
delete_workspace -> gated_vm_boundary
create_agent -> Resolver
create_agent -> pending_agent_node -> PendingAgentNode.__init__
reinit_agent -> Resolver
delete_agent -> gated_vm_boundary
grant_workspaces -> gated_vm_boundary
revoke_workspaces -> gated_vm_boundary
shell_agent -> gated_vm_boundary
exec_agent -> gated_vm_boundary
create_vm -> Resolver
reinit_vm -> Resolver
rekey_vm -> Resolver
add_git_credential -> Resolver
describe_vm -> _live_vm_boundary -> Resolver
start_vm -> _live_vm_boundary -> Resolver
start_vm -> _tailscale_rejoin_required
start_vm -> resolve_for_command(interaction=interaction)
start_vm -> _ensure_tailscale(explicit standalone auth reader)
stop_vm -> _live_vm_boundary -> Resolver
delete_vm -> _live_vm_boundary -> Resolver
backup_vm -> gated_vm_boundary
shell_vm -> gated_vm_boundary
exec_vm -> gated_vm_boundary
port_forward_vm -> gated_vm_boundary
LiveVMNode.auto_start -> _tailscale_rejoin_required
LiveVMNode.auto_start -> _ensure_tailscale(gate reader)
```

`_ensure_tailscale` is not an interaction-policy boundary and has no policy parameter, default, or
ambient fallback. It never imports or calls `resolve_for_command`, constructs a Registry, resolves a
template, or selects an auth-key name. Its required auth contract is:

```python
def _ensure_tailscale(
    db: Database,
    config: Config,
    vm: VMRow,
    platform: VMPlatform,
    ctx: RunContext,
    *,
    auth_keys: SecretReader,
    auth_key_name: str,
) -> None: ...
```

The reconnect probe is split into one non-resolving helper shared by its two callers:

```python
def _tailscale_rejoin_required(
    db: Database,
    config: Config,
    vm: VMRow,
    *,
    already_running: bool,
) -> bool: ...
```

It refreshes the VM row, waits on a known Tailscale host, returns `False` without source access when
the host reconnects, and clears a failed host before returning `True`. Only that true branch calls
`_ensure_tailscale`, which reads exactly `auth_keys.get(auth_key_name)`, opens the native transport,
rejoins, and performs the final wait. The probe accepts neither interaction policy nor a secret
source.

`start_vm` owns the standalone branch. After its first-statement policy validation, live-boundary
construction, and platform start, it enters the existing `vm_node.hold_active()` span. The reconnect
probe, conditional template/declaration lookup, standalone resolution, reader construction, and
`_ensure_tailscale` call all execute inside that one span. When the in-hold probe determines that
rejoin is required, start resolves the template's non-optional auth-key declaration exactly once
with `resolve_for_command([], config, registry, extra_decls=[decl], interaction=interaction)`. It
wraps the returned mapping in a name-scoped `SecretReader`, then calls `_ensure_tailscale` with that
reader and name. Resolution therefore remains conditional and precedes `_ensure_tailscale`; a
healthy reconnect constructs no standalone resolver or auth source. The start branch owns the
returned mapping through the ensure call. The scoped reader has no independent value copy.

`LiveVMNode.auto_start` owns the gate branch. After platform start, it enters its existing
`platform.vm_active(self._row, config=self._config)` span. The reconnect probe, conditional repair
name lookup, gate-reader access, and `_ensure_tailscale` call all remain inside that span. On the
rejoin branch it passes the unchanged `gate_secrets` reader and `repair_secret_refs()[0]` to
`_ensure_tailscale`. The node gains no `InteractionPolicy` field, parameter, derivation, or
standalone-resolution callback merely for this repair. `port_forward_vm` reaches this only
transitively through `gated_vm_boundary` and `LiveVMNode.auto_start`; there is no direct
port-forward-to-ensure or ensure-to-resolve edge.

The active hold is a lifecycle boundary, not an implementation convenience. Neither caller may
probe, prompt/read, rejoin, or wait after its hold exits. Exact event-order tests pin:

```text
standalone healthy: hold-enter, probe-false, hold-exit
standalone rejoin: hold-enter, probe-true, resolve, reader-build, ensure-enter, auth-read,
  rejoin, final-wait, ensure-return, hold-exit
gate healthy: hold-enter, probe-false, hold-exit
gate rejoin: hold-enter, probe-true, repair-name, ensure-enter, gate-reader-read, rejoin,
  final-wait, ensure-return, hold-exit
```

For each standalone failure point (probe, resolve, reader construction, and ensure) and gate failure
point (probe, repair-name lookup, gate-reader read, and ensure), tests inject `KeyboardInterrupt`
and an ordinary exception. They assert identity-preserving propagation, exactly one hold release, no
later event, and release after the failure. Failures in rejoin or final wait count as ensure
failures and obey the same ordering. A healthy probe never touches either source, and no exception
path retries resolution, source access, ensure, or hold release.

Tests pin a healthy explicit start to zero `resolve_for_command`, reader construction, and reader
access; a failed reconnect to one standalone resolution with the exact validated policy before one
ensure call; and auto-start to identity-preserving gate-reader delivery with zero standalone
resolution and no policy stored on `LiveVMNode`. An AST guard rejects optional/default auth sources,
policy or ambient-state access in `_ensure_tailscale`, `resolve_for_command` or Registry/template
resolution in its body, and either retired false edge; it also rejects policy or secret-source
parameters on the reconnect probe. An AST shape guard requires probe, conditional acquisition, and
ensure to remain lexically nested in the correct existing hold block in both callers.

### Preflight interaction subgraph

The final prediction chain has required keyword-only, no-default `interaction` at every hop:

```text
preflight_all -> require_predicted_refs -> predict_resolution -> preview_operation_resolution
```

The changed signatures are:

```python
def preflight_all(
    nodes: Iterable[Node],
    ctx: RunContext,
    *,
    registry: Registry,
    interaction: InteractionPolicy,
) -> None: ...

def require_predicted_refs(
    owner: str,
    refs: Iterable[ResourceReference],
    config: Config | None,
    registry: Registry,
    *,
    interaction: InteractionPolicy,
) -> None: ...

def predict_resolution(
    decls: Iterable[SecretDecl],
    sources: Sequence[ActiveSource],
    *,
    interaction: InteractionPolicy,
) -> dict[str, ResolutionPreview]: ...
```

Each function performs `interaction = validate_interaction_policy(interaction)` as its first
executable statement, even when the node/reference/declaration sequence is empty, and forwards that
returned local by identity. After its empty-reference fast path, `require_predicted_refs` builds one
active source tuple and passes it to `predict_resolution`, which invokes the pure operation preview
once per declaration with `interaction=interaction`; neither reads output or ambient TTY state.

The complete outward `preflight_all` call-site manifest is:

```text
sessions.manager._create._preflight_and_resolve
sessions.manager._lifecycle.resume_session
sessions.manager._scope._batch_vm_boundary
workspaces.manager.create.create_workspace
vms.manager.exec.add_git_credential
vms.manager.power.rekey_vm
vms.manager.boundary.gated_vm_boundary
vms.manager.boundary._live_vm_boundary
vms.manager.lifecycle.create_vm
vms.manager.lifecycle.reinit_vm
agents.manager.lifecycle.create_agent
agents.manager.lifecycle.reinit_agent
```

Every listed caller already receives or owns the validated operation local. It passes exactly
`interaction=interaction`; `_preflight_and_resolve` also becomes a policy-parameter internal
boundary and validates first. No caller substitutes `Resolver._interaction`, a literal, a default,
or a fresh helper result. The directed-edge manifest covers every caller above plus all three chain
edges. Reverse closure from `preview_operation_resolution` and forward closure from each caller must
equal this manifest.

Runtime tests pass every rejected interaction shape to all four chain boundaries and
`_preflight_and_resolve`; each returns the exact policy `StateError` before iterating nodes, reading
config/registry, building sources, invoking node preflight, or previewing. A positive sentinel test
walks each outward caller through the full chain and asserts object identity at every hop. Strict
mypy over production and tests remains supplemental to these AST and runtime checks.

The exact policy-parameter internal-boundary manifest is `resolve_partial_for_reveal`,
`preview_operation_resolution`, `preflight_all`, `require_predicted_refs`, `predict_resolution`,
`_preflight_and_resolve`, `_build_session_graph`, `_prepare_vm`, `_batch_vm_boundary`,
`_prepare_vm_target_for_attach`, `_rehome_vm`, `_cleanup_now_empty_workspace`,
`_cleanup_now_empty_agent`, `gated_vm_boundary`, `_live_vm_boundary`, CLI `_resume_sessions`,
`pending_workspace_node`, `PendingWorkspaceNode.__init__`, `pending_agent_node`, and
`PendingAgentNode.__init__`. Each accepts the same required keyword, validates it as its first
executable statement, and forwards or stores the returned local. `_ensure_tailscale` is
intentionally absent because it receives an already authorized `SecretReader`, not policy. The
stored-policy boundary manifest is `Resolver.resolve`, `Resolver.resolve_gate`,
`Resolver.resolve_late_repair`, `PendingWorkspaceNode.teardown`, and `PendingAgentNode.teardown`;
their first statement revalidates the stored exact object before any projection, resolution, `try`,
or cleanup. Phase 7 may add a secret-consuming root or edge only by extending the appropriate
manifest and its generated tests in the same change.

Nested teardown has no ambient-policy escape hatch. `create_session` passes its validated policy
through `_build_session_graph` into `pending_workspace_node` and `pending_agent_node`; each factory
and node constructor validates before work and stores the exact object.
`create_workspace -> pending_workspace_node` and `create_agent -> pending_agent_node` are separate
direct creation edges with the same identity requirement; their tests assert the standalone create
root's sentinel is the object stored for any later realization unwind.
`PendingWorkspaceNode.teardown` and `PendingAgentNode.teardown` revalidate the stored object as
their first statement, outside their catch-all, then pass it by identity to `delete_workspace` or
`delete_agent`. Session deletion passes its validated local into both `_cleanup_now_empty_*`
helpers; their deletion closures bind `interaction=interaction` before entering
`cleanup_now_empty_resource`'s warn-and-continue guard. Thus neither node teardown nor
empty-resource cleanup can turn a policy misuse into a rollback or best-effort warning.

`delete_vm` is the destructive catch-all pin: it validates before `_require_vm`, child checks,
confirmation, or the `try` around `_live_vm_boundary`. Its boundary receives the same exact object.
No `StateError` from policy validation can enter the best-effort platform-binding handler. Batch
resume similarly validates before its outer boundary and forwards only that local into each
per-session `try`, so a nested validation error is impossible unless the mechanical edge invariant
itself was violated.

### Test migration ledger

Tests are consumers and MUST not preserve a retired seam merely because they patch it. Phase 7
repoints the following groups:

- `tests/secrets/test_resolution_lifecycle.py` stops importing the adapter and tests the batch,
  projection, and final error mapper directly.
- `tests/secrets/test_backends.py` imports backend capability symbols from
  `agentworks.capabilities.secret_backend` and runtime symbols from `agentworks.secrets.resolve`,
  never from the secrets package root.
- `tests/secrets/test_resolver_seed.py`, `tests/test_secrets_resolver.py`,
  `tests/test_secrets_orchestration.py`, and `tests/orchestrated_fixtures.py` spy on
  `resolve_batch`, `Resolver`, or final high-level service boundaries.
- `tests/test_secrets_eager_resolve_console.py`, `tests/test_secrets_eager_resolve_sessions.py`,
  `tests/test_secrets_eager_resolve_vm_agent.py`, and
  `tests/test_vm_create_tailscale_eager_resolve.py` pass or assert the caller-derived policy while
  preserving every eager-before-mutation and no-extra-resolution ordering guarantee.
- Workspace-create coverage in `tests/workspaces/test_create_orchestrated.py`, console shell-pane
  coverage in `tests/test_secrets_eager_resolve_console.py`, ephemeral prompt coverage in
  `tests/test_session_create_ephemeral_prompts.py`,
  `tests/test_session_create_ephemeral_prompt_filtering.py`, and
  `tests/test_session_create_ephemeral_rollback.py`, VM-shell coverage in
  `tests/test_secrets_eager_resolve_vm_agent.py` and `tests/vms/test_shell_exec_orchestrated.py`,
  and add-git-credential coverage in `tests/vms/test_add_git_credential_orchestrated.py` all assert
  the required keyword and identity-preserving forward before their existing mutation or prompt
  pins.
- `tests/test_env_show.py` and `tests/test_env_show_flag.py` pass policy even when reveal is false
  and pin the partial-reveal edge when it is true. `tests/workspaces/test_lifecycle_orchestrated.py`
  covers repair, rehome, copy, delete, their internal boundary calls, and nested cleanup;
  `tests/workspaces/test_create_orchestrated.py` pins direct pending-node policy identity.
- `tests/vms/test_backup_vm.py`, `tests/vms/test_describe_vm.py`,
  `tests/vms/test_lifecycle_orchestrated.py`, `tests/vms/test_live_vm_boundary.py`,
  `tests/vms/test_delete_vm_gating.py`, `tests/vms/test_remaining_commands_orchestrated.py`,
  `tests/vms/test_ensure_tailscale_wording.py`, `tests/vms/test_vm_nodes.py`, and
  `tests/test_vm_create_tailscale_eager_resolve.py` cover every VM live, gated, power, backup,
  credential, and Tailscale edge. They pin start-owned conditional standalone resolution, required
  auth-reader delivery, gate-reader identity, no policy on `LiveVMNode`, and no standalone
  resolution in `_ensure_tailscale`. `tests/vms/test_verify_connection.py` remains outside the
  policy manifest and continues to prove it does not construct a resolver.
- `tests/agents/test_create_reinit_orchestrated.py`,
  `tests/agents/test_delete_grant_revoke_orchestrated.py`, and
  `tests/agents/test_shell_exec_orchestrated.py` cover every agent root, including standalone and
  nested delete, and pin direct create-to-pending-node policy identity.
- `tests/sessions/test_create_resume_orchestrated.py`,
  `tests/sessions/test_singular_batch_orchestrated.py`,
  `tests/sessions/test_console_attach_orchestrated.py`,
  `tests/sessions/test_delete_resource_cleanup.py`, and `tests/test_session_resume_cli.py` cover
  create, singular and batch stop/resume, delete, describe, list, attach, logs, `_prepare_vm`,
  `_batch_vm_boundary`, nested cleanup, and CLI dispatch.
- `tests/test_secrets_eager_resolve_console.py`, `tests/test_consoles_orchestration.py`,
  `tests/test_consoles_attach.py`, `tests/test_consoles_restore.py`, and
  `tests/test_consoles_shell_panes.py` cover every manifest-listed multi-console edge; create,
  delete, remove, and reorder remain explicit negative controls outside the policy graph.
- `tests/conftest.py` and `tests/_secrets_eager_support.py` expose explicit policy fixtures.
  Corresponding session suites are `tests/sessions/test_create_resume_orchestrated.py`,
  `tests/sessions/test_console_attach_orchestrated.py`,
  `tests/sessions/test_singular_batch_orchestrated.py`, and `tests/sessions/test_session_nodes.py`.
  Corresponding VM suites are `tests/vms/test_create_reinit_orchestrated.py`,
  `tests/vms/test_lifecycle_orchestrated.py`, `tests/vms/test_remaining_commands_orchestrated.py`,
  `tests/vms/test_shell_exec_orchestrated.py`, `tests/vms/test_live_vm_boundary.py`, and
  `tests/vms/test_vm_nodes.py`. Corresponding agent suites are
  `tests/agents/test_create_reinit_orchestrated.py`,
  `tests/agents/test_delete_grant_revoke_orchestrated.py`, and
  `tests/agents/test_shell_exec_orchestrated.py`. They pass policy through the top-level entry
  fixture and do not install a manager-local ambient-state shim.
- `tests/test_config_env_and_secrets.py`, `tests/plugins/test_enablement_producer.py`, and
  `tests/resources/test_readiness_fold.py` assert `active_sources` and source vocabulary.
- `tests/orchestration/test_secrets.py` migrates prediction to typed previews and no `ActiveBackend`
  annotation. `tests/orchestration/test_readiness.py`, `tests/orchestration/test_node_protocol.py`,
  `tests/sessions/test_session_nodes.py`, and `tests/vms/test_vm_nodes.py` retain readiness/node
  ordering while consuming the typed prediction and injected policy seams. They cover every
  manifest-listed `preflight_all` caller plus exact interaction identity through
  `require_predicted_refs`, `predict_resolution`, and `preview_operation_resolution`.
- `tests/test_secret_describe_no_prompt.py` rejects `resolve_batch` and client construction, not
  only the deleted wrapper.
- `tests/test_doctor_env_and_secrets.py` and `tests/test_doctor_cli.py` pin source records, the
  exact adjacent group order, degraded source placeholder, and non-probing behavior.
- `tests/guide/test_assessment.py`, `tests/guide/test_view.py`,
  `tests/guide/test_power_import_boundary.py`, and `tests/guide/test_render_service.py` replace the
  old sentinel with the permanent power seams (`resolve_batch`, verification, and client creation).
- `tests/test_secret_verify.py` is rewritten first around the outcome service and one-name Phase 7
  checkpoint, then around the Phase 8 variadic command.

Phase 7 runs strict mypy over both `cli/agentworks` and `cli/tests`; no newly required interaction
argument may be hidden behind `Any`, an untyped fixture, or a type-ignore. This is supplemental to,
not a substitute for, `tests/secrets/test_interaction_policy_graph.py`. That test owns seven literal
manifests matching the tables above: public service entries, policy-parameter internal boundaries,
stored-policy boundaries, CLI roots, directed forwarding edges, outward preflight callers, and the
two non-policy Tailscale auth-source edges plus their forbidden false edges.

For every public service and policy-parameter internal boundary, the AST guard resolves the
qualified definition, asserts a keyword-only `InteractionPolicy` parameter with no default, strips
only an optional docstring, and requires the first remaining statement to be the exact assignment
`interaction = validate_interaction_policy(interaction)`. For each stored-policy boundary it
requires `interaction = validate_interaction_policy(self._interaction)` as the first remaining
statement and requires all downstream policy uses to reference that local. It rejects any earlier
import, lookup, mutation, output call, context manager, `try`, nested function, or alternate local.
For every ordinary CLI root it requires the exact first statement
`interaction = validate_interaction_policy(ordinary_interaction_policy())`; for the verification
root it requires its already specified explicit-policy selection and validation before config.

Each directed edge records caller, callee, and keyword name. The guard requires
`interaction=interaction` on every call, including both CLI dispatch arms, both copy-workspace
boundaries, every singular/batch route, `functools.partial`/lambda cleanup bindings, and
pending-node factory, direct-create, constructor, stored-field, teardown, prediction-chain, and
deletion-callback edges. Calls may not forward an enum literal, helper result, attribute, default,
or differently named local. Tailscale's separate source-edge check requires a non-optional reader
and name, permits no policy on `_ensure_tailscale` or `LiveVMNode`, and proves standalone resolution
is owned only by `start_vm`. A reverse AST closure starts at the final core/boundary seams and fails
if it discovers a production public root or intervening edge absent from the manifests; a forward
check fails if a manifest entry no longer reaches a seam. This is how later call-graph growth
becomes a required manifest decision rather than a silent hole.

A parameterized runtime suite invokes every public service and policy-parameter internal boundary
with each rejected shape (plain string, foreign `Enum`/`StrEnum`, `str` subclass, and `.value`
lookalike). Stored-policy tests install each rejected shape into a deliberately uninitialized or
test-corrupted private field, then invoke the boundary. Every case gets exactly
`StateError("interaction must be an exact InteractionPolicy")` with no rejected value, cause, or
context, while fakes assert zero config/registry load, DB read, output, prompt, mutation,
resolver/batch/client construction, context entry, or caught warning. CLI-root tests replace the
derivation/selection helper with a wrong-type result and assert the same zero-work failure. Separate
positive edge tests use one sentinel enum object and assert identity at every hop, including nested
teardown, direct pending-node creation, preflight prediction, and cleanup callbacks. The tests
specifically prove `delete_vm` policy misuse never enters its best-effort warning handler and
pending-node policy misuse never enters a teardown catch-all. The propagation guard covers call
sites even when no retired resolver symbol appears, so neither strict mypy nor the retired-seam scan
is used as a proxy for graph completeness.

Phase 7 adds one AST guard over `cli/agentworks/**/*.py` and `cli/tests/**/*.py`. It rejects symbol
definitions, imports, attributes, and executable name loads for exactly:

```text
ActiveBackend
SecretInteractionPolicy
SecretVerification
_compatibility_error
_inspection_projection
_resolve_complete_for_legacy_callers
_verification_compatibility_error
active_backends
disabled_plugin_backends
resolve_secrets
resolve_secrets_quiet
verify_named_secret
```

It also rejects imports of exactly `agentworks.secrets.backends`, `agentworks.secrets.env_var`, and
`agentworks.secrets.prompt`; root imports of `ActiveSource`, `active_sources`, `CompletionPolicy`,
`ResolutionPolicy`, or `env_var_name_for`; and any root export of `SECRET_BACKEND_REGISTRY`. The
guard parses semantics rather than counting text. Its deny-list fixture and the negative relocation
test are the only allowed string literals for retired paths; tests and docs do not preserve retired
executable seams to make the count pass.

## Final runtime contracts

### Public diagnostic records

The outcome enums and `ResolutionOutcome` retain the exact legality table from the lifecycle LLD.
They move to `agentworks.secrets.outcomes` so the value-free contract has no import dependency on
the value-bearing runtime module:

```python
class ResolutionCategory(StrEnum): ...
class ResolutionDetail(StrEnum): ...
class ResolutionRemediation(StrEnum): ...

@dataclass(frozen=True, slots=True)
class ResolutionOutcome:
    name: str
    category: ResolutionCategory
    detail: ResolutionDetail
    remediation: ResolutionRemediation
    source: str | None = None
    identifier: str | None = None
    remediation_target: str | None = None
```

`agentworks.secrets.resolve` imports these types. `outcomes.py` imports no resolver, batch, client,
backend, Registry, output, or CLI module. This keeps verification and future JSON rendering on a
module that cannot reach values by construction.

The lifecycle LLD's central legality map also owns the structured disabled-plugin row:
`source-backend-plugin-disabled` uses `enable-plugin` and requires a plugin-identity target. No
free-form readiness reason enters this record or a renderer.

`ResolutionBatch` remains private to `agentworks.secrets.resolve`. `complete_or_raise()` remains the
only complete value projection. Verification reads only the immutable outcome tuple; partial reveal
uses one module-private projection for its explicitly authorized value cells. No generic value
accessor is added.

Consumers use ordinary direct control flow. They do not add cleanup fences, scrub primitives,
rollback-on-interrupt machinery, or intermediate owner graphs for in-memory erasure. The workstation
process is trusted, and Python local/reference lifetime is best-effort only.

### Explicit caller policy

`InteractionPolicy` moves with one shared validator to `agentworks.secrets.policy`:

```python
def validate_interaction_policy(value: object) -> InteractionPolicy:
    if type(value) is not InteractionPolicy:
        raise StateError("interaction must be an exact InteractionPolicy") from None
    return value
```

The validator never uses `repr`, string conversion, or the rejected value in its error, and the
error has no entity, hint, cause, or context. Every public boundary accepting `interaction` calls it
as its exact first executable statement, before imports, config lookup, source work, DB access,
state mutation, error-catching, or forwarding: `Resolver.__init__`, `resolve_for_command`,
`resolve_partial_for_reveal`, `verify_secrets`, and every service and internal boundary in the
recursive manifests above. The enum is package-root exported; the validator is imported from its
owning module and is not root-exported.

`CompletionPolicy` remains internal with `ResolutionPolicy` and `resolve_batch`. Internal
`ResolutionPolicy.__post_init__` requires `type(interaction) is InteractionPolicy` and
`type(completion) is CompletionPolicy`; misuse raises the same interaction error above or exactly
`StateError("completion must be an exact CompletionPolicy")` from `None`, respectively, without
rejected-value framing.

There is one CLI-owned ordinary-operation derivation helper:

```python
def ordinary_interaction_policy() -> InteractionPolicy:
    return InteractionPolicy.ALLOW if output.is_interactive() else InteractionPolicy.REFUSE
```

`output.is_interactive()` already means stdin TTY and not global non-interactive. The helper is
owned by `agentworks.cli._helpers`, is called exactly once by each top-level secret-consuming
ordinary CLI command before it enters a manager or service graph, and is not a secrets-package
export. CLI commands pass the validated enum into their public manager or service entry; internal
managers forward it unchanged. No manager, secrets service, backend, client, preview, or
`resolve_batch` call may import `output` to derive or verify policy. Direct non-CLI callers must
provide an exact enum. Tests pass an enum directly and mutate global TTY state only when testing the
CLI derivation helper.

`Resolver` becomes:

```python
class Resolver:
    def __init__(
        self,
        config: Config,
        registry: Registry,
        *,
        interaction: InteractionPolicy,
    ) -> None: ...

    def resolve(self) -> None: ...
    def resolve_gate(self, name: str) -> str: ...
    def resolve_late_repair(self, decl: SecretDecl) -> str: ...
```

There is no default policy. Production construction sites receive the already-derived enum through
their call graph. A global `--non-interactive` result is therefore frozen at the top-level operation
composition boundary and cannot change between manager, gate, boundary, and repair turns.

Every boundary-validator test passes a plain string, a foreign `Enum`/`StrEnum` with matching
members, a `str` subclass, and a lookalike object exposing `.value`; each gets the exact
`StateError` above with no rejected value, cause, or context. Positive tests prove the exact enum
object is returned and forwarded by identity. Every public-service, internal-boundary, and CLI-root
manifest entry has a generated boundary-specific pin with zero pre-validation work; none is covered
only by a representative sibling. `ResolutionPolicy` tests independently cover wrong interaction and
wrong completion types, including foreign enums and lookalikes.

### Final complete outcome-to-error mapping

`ResolutionBatch.complete_or_raise()` calls one final value-free function on incomplete results:

```python
def complete_resolution_error(
    outcomes: Sequence[ResolutionOutcome],
) -> AgentworksError: ...
```

`complete_resolution_error` is owned by `agentworks.secrets.outcomes` beside the records it formats,
imports only the stable error taxonomy, and is not package-root exported. The batch and temporary
Phase 7 one-name CLI import it from that owner.

The function requires at least one non-resolved outcome. Empty input and all-resolved input both
raise exactly `StateError("complete_resolution_error requires at least one non-resolved outcome")`
with no entity or hint. Failed outcomes stay in requested order. The first failed outcome selects
the exception type by this exhaustive map:

| First failed detail/category                                    | Error type               |
| --------------------------------------------------------------- | ------------------------ |
| `hard-mapping`                                                  | `SecretMappingError`     |
| `authentication`                                                | `ExternalError`          |
| `connectivity`                                                  | `ConnectivityError`      |
| `deadline-exceeded`                                             | `ExternalError`          |
| `external`, `malformed-value`, `backend-protocol`, `unexpected` | `ExternalError`          |
| any `unavailable` detail                                        | `SecretUnavailableError` |
| `interaction-refused`                                           | `SecretUnavailableError` |

This first-failure rule matches request-order ownership and avoids inventing a synthetic batch
severity. The exception still carries every failed outcome, value-free, in its hint. The message is
exactly:

```text
secret resolution failed for: <comma-separated failed names>
```

The hint has one line per failed outcome in request order:

```text
<name>: <category>/<detail>; source=<source-or-none>; identifier=<identifier-or-none>; remediation=<remediation>
```

Enum values provide category, detail, and remediation. Source names and identifiers have already
passed their model and control-character guards. No exception stores the outcomes, batch, client
exception, provider/client traceback, or value mapping as an attribute. The raised error necessarily
has Python's ordinary `__traceback__`; the guarantee is that no caught provider/client traceback or
exception is attached as an attribute, cause, or context. A mixed batch is therefore distinguishable
in full while retaining one ordinary `AgentworksError` at the command boundary.

Registry, source-chain, hand-built runtime contract, and late-registration failures are not
resolution outcomes. They retain their original `ConfigError`, `StateError`, or `NotFoundError` and
are never caught by this mapper.

### Operation-scoped Resolver algorithm

`Resolver.resolve()` preserves its current registration and cache rules:

1. If the cache exists, reject any newly registered name absent from it, then return.
2. Exclude gate-seeded declarations from the typed batch.
3. If no declarations remain, cache a copy of the seeds.
4. Build `active_sources` once and call `resolve_batch` once with `COMPLETE`, the resolver's frozen
   interaction policy, and an output interaction broker only under `ALLOW`.
5. Call `complete_or_raise()`, join the returned values with gate seeds, and publish the completed
   operation cache.
6. Return after publication. Incomplete resolution raises before a cache is installed.
7. Keep `get`, `values`, `resolved`, first-registration-wins, and late-registration errors
   unchanged.

The batch is not the long-lived cache. Its job is typed computation and fail-closed completion; the
dictionary is the operation's authorized value channel to `ScopedSecrets`. This is not the deleted
dict compatibility adapter. It is the existing operation cache, obtained only after a complete
batch.

`resolve_for_command` remains the high-level standalone operation service because several session
and console flows legitimately return values to `compose_env`. Its signature gains a required
keyword-only `interaction: InteractionPolicy`. It computes declarations, returns `{}` without
building active sources when none are needed, otherwise calls one `resolve_batch(... COMPLETE ...)`
and returns `complete_or_raise()`. It contains no parallel resolution loop or ambient interaction
read.

### Gate and late-repair sequencing

`Resolver.resolve_gate(name)` is the only pre-boundary point lookup:

1. Refuse if the boundary cache already exists.
2. Register or recover the declaration by name.
3. If already seeded, return the existing seed.
4. Resolve the singleton declaration through the same active sources, frozen interaction policy,
   broker rule, complete policy, and outcome-to-error mapping.
5. Project the complete singleton result, validate the exact key, seed it, and return the value.

`gate_secret_resolver` becomes a narrow callable adapter over this method. It contains no source or
batch imports.

`Resolver.resolve_late_repair(decl)` exists only for the session batch gate's rejoin repair key. It
requires that the boundary cache already exists, resolves exactly the supplied declaration through
the same frozen policy, validates the complete singleton result, and returns its value. It does not
register, seed, or widen the cache. The existing `_scope.py` check that the name belongs to that VM
node's `repair_secret_refs()` remains the authority that permits the call. Guard tests allow this
one production call site and reject every other call. All other registration after `resolve()`
remains an error.

## Pure preview and inspection contracts

### Preview records

`agentworks.secrets.preview` owns the pure records and algorithm:

```python
class PreviewCategory(StrEnum):
    ATTEMPTABLE = "attemptable"
    REFUSED_INTERACTION = "refused-interaction"
    UNAVAILABLE = "unavailable"

@dataclass(frozen=True, slots=True)
class SkippedSource:
    source: str
    reason: str

@dataclass(frozen=True, slots=True)
class ResolutionPreview:
    name: str
    category: PreviewCategory
    source: str | None
    identifier: str | None
    skipped_not_ready: tuple[SkippedSource, ...]
```

Legality is enforced in `__post_init__`: `ATTEMPTABLE` and `REFUSED_INTERACTION` require `source`;
`UNAVAILABLE` forbids source and identifier. Every skipped source has a non-empty readiness reason.
All strings pass the same control/format-character guard used by outcomes.

Two entry points share one source walk:

```python
def preview_resolution(
    secret: SecretDecl,
    sources: Sequence[ActiveSource],
) -> ResolutionPreview: ...

def preview_operation_resolution(
    secret: SecretDecl,
    sources: Sequence[ActiveSource],
    *,
    interaction: InteractionPolicy,
) -> ResolutionPreview: ...
```

The walk calls only source mapping selection, mapping validation, `would_attempt`,
`describe_lookup`, and stored readiness. It never calls `resolve_batch`, `create_client`, a client
method, output, environment lookup, subprocess, or broker. It accumulates would-attempt sources that
are not ready. The inspection form returns the first ready would-attempt source as `ATTEMPTABLE`,
including an interactive source. The operation form skips refused interactive sources while looking
for a later ready non-interactive candidate; if none exists but at least one ready interactive
candidate was skipped, it returns `REFUSED_INTERACTION` naming the first. Otherwise it returns
`UNAVAILABLE`.

This is applicability prediction, not proof. Renderers say `would attempt via`, never
`would resolve via`. An env-var source may be attemptable while its variable is unset. Actual
presence, authentication, transport, hard misses, and values belong only to resolution or
verification.

`orchestration.secrets.predict_resolution` returns `dict[str, ResolutionPreview]` using
`preview_operation_resolution` and the caller's validated operation policy, forwarded through the
exact preflight chain above. Resolver-owning roots pass the same object they stored in the Resolver;
the preflight chain never reads a private Resolver field. `require_predicted_refs` accepts only
`ATTEMPTABLE`; its failure hint names the preview category and still points to
`agw secret describe NAME`. This preflight is a pure impossibility screen. The typed boundary
remains authoritative for soft misses and provider failures.

### Secret list and describe records

Inspection vocabulary becomes source-first:

```python
@dataclass(frozen=True, slots=True)
class SecretSourceCell:
    source: str
    would_attempt: bool
    identifier: str | None
    not_ready_reason: str | None

@dataclass(frozen=True, slots=True)
class SecretTable:
    sources: tuple[str, ...]
    rows: tuple[SecretRow, ...]
    operator_count: int
    auto_count: int
```

`SecretSourceCell` contains only fields consumed by the list header/cell renderer. Backend identity
and provenance are deliberately absent because list does not render them; carrying unused copies
would create a second ownership surface. `BackendMapping` is renamed `SourceMapping` and separately
owns `source`, `backend`, `provenance`, `would_attempt`, `identifier`, and `not_ready_reason` for
describe. `SecretDescription.backend_mappings` is renamed `source_mappings`, while the rendered
section keeps the manifest field's truthful public spelling `Backend mappings:`. The section line
is:

```text
- <source> (<backend>, <provenance>): <identifier-or-status>
```

The override provenance value renders as `operator override of synthesized default`; the other
values render as `synthesized default` and `declared`. This makes replacement of `env-var` or
`prompt` visible without a shadow row.

`SecretDescription.resolution` is the typed `ResolutionPreview`. The renderer first lists skipped
not-ready sources, then prints `would attempt via <source>` or
`not attemptable through any active source`. It never says available or resolved. The redundant
`available: bool` is deleted.

List and describe build `active_sources` once, call only the pure projection, and never catch a
backend protocol error as an ordinary unavailable answer. A pure backend contract violation becomes
a value-free `StateError` naming source and operation, because installed backend code violated a
framework invariant.

The list table headers are exactly `NAME`, `DESCRIPTION`, then one source name per active chain
entry in precedence order. A source cell uses this exclusive precedence and exact grammar:

1. `won't attempt` when mapping selection opts out or the backend requires an absent mapping;
2. `not ready: <reason>` when it would attempt but folded readiness is not ready, even if an
   identifier exists;
3. the safe identifier when ready, attemptable, and statically identifiable;
4. `would attempt` when ready and attemptable with no static identifier.

No registry secrets renders `No secrets in the resource registry.` An empty active source chain with
registry secrets renders exactly:

```text
No active secret sources. Set [secret_config].backends to source names (or leave it unset to use env-var then prompt).
```

The old `No active secret backends.` wording is removed. `--names-only` remains one full name per
line and bypasses table cells.

### Doctor records and boundaries

Doctor keeps the existing `Secret backends` capability-readiness group and adds a `Secret sources`
group built from every present source row:

```python
@dataclass(frozen=True, slots=True)
class SecretSourceStatus:
    name: str
    backend: str
    provenance: SourceProvenance
    active: bool
    enabled: bool
    not_ready_reason: str | None
```

The source group reads only Registry rows, graph enablement/readiness, config chain membership, and
`source_provenance`. It performs no capability class operation. Each row renders backend, active or
inactive, enabled or disabled, provenance, and ready or the folded not-ready reason. All rows are
shown, including disabled and inactive rows, because this group explains why a declared source does
or does not participate. The plugin roster remains the authority for enabling a system plugin, but
the source row still truthfully reports its dependency state.

Report order is exact and adjacent: `Secret backends`, then `Secret sources`, then `Secrets`, with
no group inserted between them. In config or Registry degraded mode all three positions remain. The
new middle position is `_skipped_group("Secret sources", "Declared sources")`, whose single row is
`Declared sources: skipped (config or manifests unavailable; see the Configuration group)`. Healthy
and degraded `run_checks` tests pin the adjacent names and the degraded placeholder rather than
calling only the individual builders.

The `Secrets` group consumes `preview_resolution` for each declaration. It renders
`would attempt via <source>` for `ATTEMPTABLE` and warns `not attemptable through any active source`
otherwise, including skipped source reasons. It does not pass an interaction policy because doctor
never opens the interactive source it reports. Doctor never calls the typed resolution core.

### Explicit partial value reveal

`agw env show --resolve` receives:

```python
@dataclass(slots=True)
class PartialResolution:
    values: dict[str, str]
    outcomes: tuple[ResolutionOutcome, ...]

def resolve_partial_for_reveal(
    secrets: Sequence[SecretDecl],
    sources: Sequence[ActiveSource],
    *,
    interaction: InteractionPolicy,
) -> PartialResolution: ...
```

The record and service are owned by `agentworks.secrets.resolve`, are not package-root exports, and
exist only for the explicit env reveal caller. It has a redacted `repr`, no serializer, and never
reaches an output handler as a whole. The env renderer builds an outcome map for non-resolved names
and renders the same safe line used by operation hints:
`<category>/<detail>; source=...; identifier=...; remediation=...`. It reads `values` only for an
explicit reveal cell. The `errors: dict[str, str]` out-parameter and all legacy free-form resolution
strings are deleted.

The partial service receives the caller-derived interaction policy, calls `resolve_batch` with
explicit `PARTIAL`, and uses its module-private projection to build the explicit reveal record. No
generic batch value accessor or ambient-state read is added.

## Final verification surface

### Service contract

Phase 7 replaces `SecretVerification`, `SecretInteractionPolicy`, and `verify_named_secret` with:

```python
def verify_secrets(
    config: Config,
    registry: Registry,
    names: Sequence[str],
    *,
    interaction: InteractionPolicy,
) -> tuple[ResolutionOutcome, ...]: ...
```

`interaction` has no default and must be an exact enum. The service does not import `output`,
inspect stdin, or read global `--non-interactive`; direct callers own the policy they explicitly
pass.

The algorithm is exact:

```python
_INVALID_NAME_ERROR = (
    "invalid secret name; expected 1-253 lowercase alphanumeric characters, "
    "hyphens or underscores, with an alphanumeric first and last character "
    "and no consecutive hyphens"
)
```

1. Require at least one name for non-CLI callers, otherwise raise exactly
   `ValidationError("at least one secret name is required")` with no entity or hint.
2. Validate every supplied item in request order with
   `validate_name(name, max_length=MAX_SECRET_NAME_LENGTH)` from `agentworks.naming`, before
   deduplication, lookup, rendering, or source work. A non-`str` item or any canonical validation
   failure is caught and replaced from `None` with exactly `ValidationError(_INVALID_NAME_ERROR)`,
   with no entity, hint, rejected name, cause, or context.
3. Deduplicate in first-encounter order. The first occurrence owns ordering and prompt metadata.
4. Look up every unique name before building active sources. The first missing name raises
   `NotFoundError("secret '<name>' not found", entity_kind="secret", entity_name=name)` and no
   source client runs.
5. Build the active source tuple once.
6. Call `resolve_batch` once with `COMPLETE` and an output broker only for `ALLOW`.
7. Return the immutable outcome tuple, resolved or not. Values remain private to the unreturned
   batch and become ordinary unreachable process memory when the service returns.

Verification does not call `complete_or_raise`, translate provider results into legacy errors, or
retain a success boolean. A resolved outcome is the proof record. The old service and proof type are
deleted in Phase 7. Through the Phase 7 checkpoint, the existing one-name CLI maps its pre-release
flag to `InteractionPolicy` at the CLI boundary, rejects flag plus global non-interactive there,
calls `verify_secrets(..., [name], ...)`, raises `complete_resolution_error(outcomes)` when its sole
outcome is not resolved, and retains its generic success line otherwise. This is presentation-only
compatibility: no old service, result type, policy type, resolver wrapper, or value dictionary
survives it. Phase 8 replaces that presentation with the final table and exit rule.

### CLI syntax and interaction

Phase 8 changes `cli/commands/secret.py` to:

```python
@secret_app.command("verify")
def secret_verify(
    names: list[str] = typer.Argument(..., help="Secret names to verify."),
    allow_interaction: bool = typer.Option(
        False,
        "--allow-interaction",
        help="Allow sources that may prompt, authenticate, or require operator presence.",
    ),
) -> None: ...
```

Before config or Registry loading, the CLI derives verification policy exactly once. Without the
flag it selects `REFUSE` without consulting TTY or global state. With the flag, it checks
`output.non_interactive()`; when true it raises exactly
`ValidationError("--allow-interaction cannot be used with --non-interactive")`, otherwise it selects
`ALLOW`. It calls the service once, renders outcomes, and raises `typer.Exit(1)` only after
rendering when any category is not `resolved`. It contains no registry lookup or resolution logic,
and the service does not duplicate this CLI-global validation.

Default verification refuses prompt, OnePassword biometric or reauthentication, and interactive
plugins even on a TTY. `--allow-interaction` opts into all such source turns subject to fail-before-
interaction doom. Global `--non-interactive` makes the CLI reject the opt-in with a clean exit 1
before config loading or any client. Without the opt-in, global non-interactive is redundant and
legal. A direct service caller may pass `ALLOW` regardless of CLI global state; its own host and
broker contract govern that call.

### Human renderer and exits

`render_verification(outcomes)` receives only `tuple[ResolutionOutcome, ...]` and emits a table with
these exact columns in request order:

```text
NAME  CATEGORY  SOURCE  IDENTIFIER  DETAIL  REMEDIATION
```

Missing optional fields render as `-`. The renderer does not replace enum values with free-form
provider text. A one-name success is therefore a row, not the old `Secret '<name>' verified.` line.

Exit behavior is:

| Condition                                       | Rendering                                                       | Exit                |
| ----------------------------------------------- | --------------------------------------------------------------- | ------------------- |
| No names                                        | Click's ordinary missing-argument usage error; no outcome table | 2                   |
| Every unique name resolved                      | one `resolved` row per unique name                              | 0                   |
| Any unavailable/refused/timeout/failure outcome | all outcome rows, including successes                           | 1                   |
| Duplicate argv names                            | one lookup, attempt, and row for the first occurrence           | as its unique batch |
| Invalid or unsafe name                          | safe canonical-name `ValidationError`; no echoed name or table  | 1                   |
| Unknown secret name                             | ordinary `NotFoundError`; no outcome table and no client        | 1                   |
| Both interaction flags                          | exact CLI `ValidationError`; no config load, table, or client   | 1                   |
| Config/registry/source-chain error              | ordinary typed CLI framing; no outcome table                    | 1                   |
| `UserAbort` or EOF from an allowed prompt       | existing `Aborted.` control-flow rendering                      | 1                   |
| Keyboard interrupt                              | existing cleanup and exit 130 behavior                          | 130                 |

The table and exit are deterministic for mixed batches. A failed row cannot suppress a successful
row, and no error taxonomy selects only the first outcome on this surface.

## Package exports and dependency direction

The final `agentworks.secrets.__all__` contains:

```text
InteractionPolicy
ResolutionCategory
ResolutionDetail
ResolutionOutcome
ResolutionRemediation
SecretConfig
SecretDecl
SecretSourceDecl
SecretTarget
compute_needed_secrets
guide_contributions
publish_builtin_secret_sources
resolve_for_command
validate_chain
```

`CompletionPolicy` and `ResolutionPolicy` stay internal in `agentworks.secrets.resolve` beside
`resolve_batch`. `ActiveSource`, `active_sources`, `ResolutionBatch`, `resolve_batch`, preview
implementation types, partial reveal records, and output brokers likewise stay module-internal and
are imported by their truthful owner modules, not flattened at package root. Only
`InteractionPolicy` is root-exported because public operation and verification services require
direct non-CLI callers to state it. `Resolver` remains owned at
`agentworks.secrets.resolver.Resolver`. `verify_secrets` remains owned at
`agentworks.secrets.verification.verify_secrets`.

The following root exports are deleted: `ActiveBackend`, `ActiveSource`, `active_backends`,
`active_sources`, `resolve_secrets`, and `env_var_name_for`. The latter is already truthfully public
from `agentworks.capabilities.secret_backend`; production callers use that owner. No old module path
or import alias is retained.

## Completion, documentation, and guide ownership

### Phase 7 lockstep updates

Phase 7 updates permanent material for behavior it makes true:

- `cli/agentworks/secrets/README.md`: operation policy, typed errors, pure preview, source
  provenance, doctor boundaries, and the explicit partial reveal exception;
- `cli/README.md`: replace backend vocabulary in operation, list, describe, and doctor teaching; say
  inspection predicts attemptability without reading values;
- root `README.md` and `docs/why-agentworks.md`: replace backend-chain/runtime wording with the
  source declaration and operation-boundary model;
- `docs/guides/resources.md`: the same operator teaching, including the new source status group and
  override provenance;
- `docs/guides/upgrading-to-0.14.md`: keep the direct-backend break and exact source rewrite aligned
  with the final consumer vocabulary;
- `docs/adrs/0013-cli-side-secret-injection.md`,
  `docs/adrs/0019-orchestration-layer-command-plans-over-node-graphs.md`, and
  `docs/adrs/0022-single-resource-declaration-frontend.md`: update current implementation notes and
  links without rewriting the historical decision;
- `cli/agentworks/orchestration/README.md` and `cli/agentworks/capabilities/README.md`: explicit
  injected policy, source-first prediction, and capability-versus-resource ownership;
- `cli/agentworks/capabilities/vm_platform/README.md`: replace active-backend-chain runtime teaching
  with active source chain plus injected interaction policy;
- `cli/agentworks/capabilities/base.py`, `cli/agentworks/capabilities/git_credential/base.py`,
  `cli/agentworks/plugins/proxmox/platform.py`, and `cli/agentworks/vms/nodes.py`: update the named
  source-chain and conditional-resolution docstrings without weakening capability boundaries;
- remaining source-chain docstrings in `cli/agentworks/vms/manager/{power,tailscale,_helpers}.py`
  and `cli/agentworks/sessions/manager/{_env,_scope}.py`: use source identity and caller-injected
  policy, retaining backend only when describing an implementation capability;
- `cli/agentworks/errors.py`: taxonomy docstrings match the final outcome-to-error map, including
  authentication as `ExternalError` and connectivity alone as `ConnectivityError`;
- `cli/agentworks/secrets/guide-content/concept-secrets/{overview,teaching,agent-contract}.md`:
  sources rather than backends as configured instances, preview is not proof, and resolution still
  requires consent;
- `cli/agentworks/secrets/guide_contributions.py`: its universal-topic summary uses source and typed
  proof vocabulary rather than the retired backend resolver;
- local docstrings in resolver, orchestration, inspection, doctor, env reveal, and config reference
  sites that currently teach a live `resolve_secrets` or active-backend surface.

A repo-wide semantic vocabulary test scans production Python docstrings/comments and permanent
Markdown for live runtime uses of `active backend`, `active backends`, `backend chain`,
`resolve_secrets`, and `ActiveBackend`. It permits `secret-backend` and backend terminology only
when the sentence is actually about an implementation capability, and excludes historical/locked SDD
artifacts rather than rewriting history. Rendered universal-guide tests load every concept-secrets
block and assert source-chain, injected-policy, preview-versus-proof, and consent wording while
rejecting the retired runtime phrases. This scan and rendered coverage land with Phase 7 docs, not
as Phase 8 polish.

Phase 7 does not publish the final variadic verify syntax before it exists. Existing verify teaching
may remain single-name through the Phase 7 checkpoint, but its implementation uses outcomes.

### Phase 8 command, guide, and completion ownership

Phase 8 updates in the same commit as the command cutover:

- add a Secrets command table under `cli/README.md`'s command reference with `list`, `describe`, and
  `verify NAME... [--allow-interaction]`;
- update the detailed CLI secret section, `cli/agentworks/secrets/README.md`, and
  `docs/guides/resources.md` to the variadic syntax, final flag, row columns, interaction
  precedence, and exit semantics;
- update the package-owned concept-secrets teaching and agent contract so an agent asks consent
  before adding `--allow-interaction` and never treats guide rendering as consent;
- keep the guide action/evidence contract named `verify-named-secret` unchanged. It records one
  named proof supplied by the caller and does not mirror CLI batching;
- change `DYNAMIC_COMPLETIONS[("secret.verify", "name")]` to
  `DYNAMIC_COMPLETIONS[("secret.verify", "names")] = "secrets"`;
- rely on the existing `nargs == -1` to `ParamSpec.multiple=True` normalization, then generate and
  test Bash (`-ge`), Zsh (`*:`), and PowerShell (`-ge`) completion for the second and later secret
  positions as well as the first;
- pin `--allow-interaction` present and `--allow-interactive` absent in the introspected command
  tree and all generated scripts.

The shell source stays `agw secret list --names-only`, so completion remains value-free and does no
resolution. No sample config change is required in Phases 7 or 8: the source declaration and chain
samples already landed with the model cutover, and this work changes consumers and command shape,
not configuration.

## Phase 7 and Phase 8 sequencing

### Phase 7: consumer migration

One always-green Phase 7 commit may use private helpers while it is being assembled, but its final
state MUST:

1. move outcomes into the value-free owner and add the final complete error projection;
2. migrate the complete recursive service/CLI/edge manifests, `Resolver`, gate seeding, authorized
   late repair, standalone resolution, and partial reveal to first-statement-validated,
   identity-forwarded caller policy, including the complete preflight chain; move conditional
   Tailscale standalone resolution to `start_vm` and require an explicit auth reader at ensure; use
   typed batches without consumer-side memory-erasure machinery;
3. replace string/none preview with pure typed previews and migrate orchestration prediction;
4. migrate list, describe, doctor, and env reveal records and rendering;
5. migrate the already-existing verification service to validated names, all outcomes, and its
   cleanup fence while its CLI may retain the one-name checkpoint shape;
6. remove aliases, dict/error/quiet wrappers, `disabled_plugin_backends`, old root exports, and
   every stale consumer;
7. add the manifest-driven signature, first-statement, reverse-closure, forwarding-edge, runtime
   misuse, semantic retired-path, and one-late-repair-call-site guards; then update the complete
   Phase 7 permanent-doc inventory.

At that boundary there is no `resolve_secrets*` symbol. The single-name verify command is not a
reason to retain it because verification already consumes the final typed result.

### Phase 8: verification command completion

Phase 8 then:

1. changes the existing Typer parameter from singular to required variadic names;
2. renames the pre-release interaction flag with no alias;
3. renders every `ResolutionOutcome` and applies the exact exit table;
4. updates the command reference, secrets docs, resources guide, and package-owned guide blocks;
5. repoints dynamic completion to `names` and pins repeated completion in all three shells.

No second verification result type, second resolution pass, JSON flag, backend-authored rendering,
or value-bearing renderer is introduced. Future JSON output serializes the same `ResolutionOutcome`
fields after a separate interface decision.

## Security and failure invariants

- Every operation, gate, repair, inspection reveal, and verification request calls the one typed
  core. No consumer reproduces source-turn or provider failure logic.
- A batch is complete before values join an operation cache. Incomplete resolution exposes no value
  through its public result or exception object.
- Verification returns outcomes only. Mixed success never makes a success value reachable from its
  renderer.
- Preview, list, describe, doctor, guide, schema, and completion never read a secret or construct a
  client. Tests patch client factories, environment reads, subprocess, broker calls, and
  `resolve_batch` to fail if crossed.
- Operation and verification policies are immutable explicit inputs below top-level CLI composition.
  Managers, services, backends, and clients do not inspect TTY or global flags.
- Both Tailscale repair paths retain their existing active hold across reconnect probing,
  conditional auth-key acquisition, rejoin/final wait, and caller-owned value cleanup. Hold release
  is last on success and exception; no probe, prompt/read, ensure step, or cleanup escapes the hold.
- Errors and rows contain only validated name, category, detail, remediation, source, and safe
  identifier fields. Provider messages, stderr, exceptions, provider/client tracebacks, and values
  never enter; ordinary Python `__traceback__` ownership remains unchanged.
- The workstation process is trusted. In-memory reference lifetime and traceback locals are
  best-effort only; application-layer ownership fences, frame rewriting, and adversarial memory
  erasure are out of scope. Strong erasure, if ever required, belongs in an isolated process whose
  address space exits.
- Source override provenance is derived from the surviving Registry row, never stored as a second
  flag.
- Partial env reveal holds values only in the explicit reveal record and rendered value cell.
  Failure cells consume outcomes only.
- `UserAbort`, `KeyboardInterrupt`, `SystemExit`, and `GeneratorExit` preserve the lifecycle LLD's
  cleanup and propagation behavior. No consumer converts them to resolution outcomes.

## Exhaustive test matrix

Tests verify observable behavior and exception-object hygiene. They do not inject line-level
interruptions to inspect local reference lifetime or traceback frames.

| Area                     | Required coverage                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Outcome module           | exact existing legality table; frozen/slotted/value-free; no import path to batch/client/output; root exports are exact                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Error mapping            | every detail to exact exception type, including authentication to ExternalError and connectivity to ConnectivityError; first failed outcome owns type; all failed lines retained in order; resolved rows omitted; empty/all-resolved exact StateError/message; safe framing; no values/provider text/cause/context                                                                                                                                                                                                                                                        |
| Resolver                 | required injected policy; one active-chain build and batch; seed exclusion/join; empty missing set; complete failure caches nothing; idempotence; late registration; operation cache copy; no second prompt                                                                                                                                                                                                                                                                                                                                                               |
| Gate                     | pre-boundary only; singleton batch; repeat seed no re-read; same policy/broker/error map; exact singleton validation; no wrapper imports                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| Late repair              | boundary must have run; declared repair succeeds without cache widening; unauthorized site refuses before call; exactly one production call site; no seed or registration                                                                                                                                                                                                                                                                                                                                                                                                 |
| Standalone operation     | empty target fast path; injected policy; target union and extras; one typed batch; complete-or-raise; callers forward policy                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Policy validation        | shared validator uses exact-type identity; exact enum returns unchanged; every service and internal-boundary manifest entry rejects strings, foreign Enum/StrEnum values, str subclasses, and `.value` lookalikes as its first action with exact StateError/message, no rejected value/cause/context, and zero work; every CLI root rejects a wrong derived value before work; core independently rejects wrong completion with its exact error                                                                                                                           |
| Policy propagation       | ordinary secret-consuming CLI derives once: TTY plus no global flag allows, pipe/no TTY refuses, global refusal wins; manifest-driven AST validates signatures, exact first statements, reverse/forward closure, every edge keyword, and sentinel identity; strict mypy covers production and tests only as a supplement; managers/services/core/backend/client never read ambient state; verification CLI rule is separate                                                                                                                                               |
| Preflight policy         | all twelve outward call sites pass the same local through first-statement-validated `preflight_all`, `require_predicted_refs`, `predict_resolution`, and `preview_operation_resolution`; empty inputs still validate; wrong types do zero work; reverse and forward closure are exact; no ambient read or Resolver-private substitution                                                                                                                                                                                                                                   |
| Tailscale auth source    | start keeps probe, conditional standalone resolution/reader build, and ensure inside `vm_node.hold_active()`; auto-start keeps probe, conditional repair-name/gate-reader access, and ensure inside `platform.vm_active()`; exact healthy/rejoin event order; failures preserve the existing hold lifecycle; AST nesting guard; healthy start performs no source access; ensure requires reader+name and has no policy/default/Registry/template/standalone path; gate reader forwards by identity with no policy storage; port-forward has no direct ensure/resolve edge |
| Nested/destructive paths | create-session pending workspace/agent nodes validate, store, revalidate outside catch-all, and identity-forward policy into nested delete; session-delete cleanup helpers bind the same local before warn-and-continue; batch resume forwards before per-item catches; delete_vm validates before lookup and best-effort try; wrong-type tests assert no mutation, teardown conversion, warning, prompt, or resolver construction                                                                                                                                        |
| Preview legality         | exact category/source/identifier invariants; safe strings; immutable skipped records                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Preview purity           | no environment read, factory, client, subprocess, broker, output, or typed batch; mapping validation/would-attempt/identifier only; protocol violation is StateError                                                                                                                                                                                                                                                                                                                                                                                                      |
| Preview semantics        | first ready attemptable; not-ready skip order; interactive optimistic inspection; refused operation candidate with later non-interactive fallthrough; refused terminal; no candidate unavailable                                                                                                                                                                                                                                                                                                                                                                          |
| Orchestration prediction | typed record per declaration; actual policy; impossible refs fail with category; env-var attemptability is not claimed proof; boundary stays authoritative                                                                                                                                                                                                                                                                                                                                                                                                                |
| List                     | exact NAME/DESCRIPTION/source headers and order; exact four-state cell precedence/grammar; exact no-secret and no-active-source wording; cell record has only rendered fields and no backend/provenance copies; names-only remains value-free and no render work                                                                                                                                                                                                                                                                                                          |
| Describe                 | source mappings; exact provenance phrases; pure preview wording; skipped readiness; no redundant boolean; NotFound unchanged; no clients or values                                                                                                                                                                                                                                                                                                                                                                                                                        |
| Doctor sources           | synthesized, override, and declared provenance; active/inactive; enabled/disabled; folded ready/not-ready; all present sources; exact adjacent backends/sources/secrets order; exact degraded placeholder; no backend method or client                                                                                                                                                                                                                                                                                                                                    |
| Doctor secrets           | one row per registry secret; attemptable/unavailable wording; skipped readiness; prompt optimistic but unopened; env-var unopened; no values                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Partial reveal           | separate values/outcomes; partial success; every failure category; inline safe format; redacted record; only explicit cells reveal sentinels                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Retired seams            | exact semantic AST deny-list for twelve symbols, three old modules, forbidden root runtime/policy imports and root registry export; dead disabled helper absent; negative relocation fixture remains                                                                                                                                                                                                                                                                                                                                                                      |
| Verify service           | required exact policy; empty input; validate every item before dedupe/lookup; non-str, empty, long, double-hyphen, control/newline exact safe ValidationError with no echo/cause; first-order dedupe; one chain/batch; all outcomes; no ambient read                                                                                                                                                                                                                                                                                                                      |
| Verify CLI               | zero names usage exit 2; one/many/duplicates/mixed/all categories; invalid/control/newline names safe and absent from stdout/stderr; unknown/config failure; exact table; success 0/any outcome failure 1                                                                                                                                                                                                                                                                                                                                                                 |
| Verify interaction       | default REFUSE without TTY read; explicit allow; global refusal plus allow exact CLI error before config; direct service ALLOW ignores CLI ambient state; prompt/OnePassword/plugin opt-in; doom before interaction; abort 1; interrupt 130                                                                                                                                                                                                                                                                                                                               |
| Verify disclosure        | distinct sentinels in every client boundary, success value, provider failure, source config, and prompt metadata absent from rows, stdout/stderr, errors, logs, repr, and exception-object cause/context                                                                                                                                                                                                                                                                                                                                                                  |
| Command shape            | required variadic `names`; final flag present; pre-release flag and singular result line absent; CLI remains thin                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Completions              | command name; dynamic key names real variadic param; `multiple=True`; first, second, and later candidates in Bash, Zsh, PowerShell; list names-only source; removed flag absent                                                                                                                                                                                                                                                                                                                                                                                           |
| Docs and guide           | source vocabulary; pure preview versus proof; final syntax/flag/exits; consent language; universal topic contribution loads; no permanent SDD links                                                                                                                                                                                                                                                                                                                                                                                                                       |

## Completion boundary

This LLD is complete when implementation can migrate every consumer without choosing an error type,
policy owner, preview meaning, doctor probing boundary, source provenance rendering, verification
syntax, result record, exit, export, completion key, documentation owner, or Phase 7 versus Phase 8
cut. Deletion is mechanically provable when the retired-seam scan is empty and every remaining
value-producing path is either `ResolutionBatch.complete_or_raise()` at an authorized operation
boundary or the explicit partial env reveal projection.
