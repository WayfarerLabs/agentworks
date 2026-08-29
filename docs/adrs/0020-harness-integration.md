# 20. Model a Session's Workload as a Harness Integration Capability

Date: 2026-07-19

## Status

Accepted. Builds on the capability / declarable-resource split of
[ADR 0016](0016-yaml-resource-manifests.md) and the orchestration layer of
[ADR 0019](0019-orchestration-layer-command-plans-over-node-graphs.md); the capability lifecycle
contract it relies on (validate / construct / preflight / runup / ops) is documented in
`cli/agentworks/capabilities/README.md`.

> Note (2026-07-30): the capability model described here is unchanged, but the `claude-code`
> integration no longer ships in the core. It moved into the opt-in `claude` system plugin (the
> plugin-system effort); `shell` remains the built-in default integration, and `claude-code` is
> present-but-disabled until an operator sets `[plugins] system = ["claude"]`. Where this record
> calls `claude-code` a "built-in", read "shipped integration": it is now a system-plugin
> capability, not a core one.
>
> Note (2026-08-01): the per-session integration state blob (now the
> `sessions.harness_integration_state` column) is NAMESPACED by integration name: the row stores
> `{"<integration-name>": {<that integration's keys>}}`, and the platform seam
> (`sessions/nodes._harness_integration_for_template`) hands each integration only its own namespace
> as `self._state`, making cross-integration key collisions on a template's integration switch
> structurally impossible. The authoring contract is unchanged. Pre-namespacing rows (claude-code's
> `session_id` at the top level) are adopted lazily by a legacy hoist
> (`HarnessIntegration.hoist_legacy_state`, overridden by `claude-code`), compatibility code slated
> for deletion at the next major release.
>
> Note (2026-08-04): the capability is now named `harness-integration`. In agent-systems usage,
> Claude Code and Codex are harnesses; Agentworks supplies integrations that run those harnesses.
> The rename changes the kind, selector, identifiers, and persisted column without changing the
> model or behavior decided here.
>
> Note (2026-08-04): `session resume` is now the canonical lifecycle command and integration method.
> `session restart` and the `restart_command`, `harness`, and `harness_config` compatibility inputs
> were removed in 0.14.0. `session resume`, `resume_command`, and `harness_integration` are the
> accepted surfaces. The older vocabulary below records the decision as it stood when this ADR was
> accepted.
>
> Note (2026-08-28): ADR 0023 and harness-integration contract version 2 supersede this record's
> same-integration `merge_config` mechanism with schema-directed model policy. Selector silence and
> integration-switch reset remain unchanged. The `merge_config` language below records the accepted
> version-1 decision; a version-1 third-party integration is now refused at registration.

## Context

A session's runtime was three flat fields on the session template: `command`, `restart_command`, and
`required_commands`. `session create` built the pane command imperatively
(`_build_session_command`), and the launch-target readiness (does every required command exist for
the user the session runs as?) lived in a hand-rolled `RequiredCommandsCheck` held by the session
node. That shape had two problems:

1. **A session's workload was stringly configured, not a modeled thing.** Running Claude Code, a
   plain shell, or a future runtime meant hand-writing a command line per template. There was no
   place for a runtime to own its own vocabulary (a resume-vs-launch decision, permission-mode
   flags), its own required tools, or its own readiness, and no uniform way for the registry to list
   what runtimes the system supports.
2. **The readiness and the op string were bespoke to sessions**, duplicating machinery the
   capability model already generalizes: config validation, a declared-secret contract, the
   preflight/runup readiness split, and inheritance-time config merge.

The capability model (ADR 0016) and the orchestration layer (ADR 0019) had by this point generalized
exactly this shape for VM platforms and git-credential providers: a capability validates its config
block, declares the secrets it names, owns its readiness verbs, and produces its domain ops, while a
consuming resource holds an instance and composes it into the node graph. A session's workload is
the same shape.

## Decision

Model a session's workload as a **`harness-integration` capability**, and make the session the rich
consuming resource that holds one.

- **The `harness-integration` capability kind.** A new capability kind (`harness-integration`,
  category `capability`, `miss_policy="error"`), mirroring `git-credential-provider`. The built-ins
  are `shell` (run an operator command, or a bare login shell) and `claude-code` (launch or resume a
  Claude Code session). Each is a read-only registry row that lists, describes, and is referenced
  like any other resource.
- **A session is a specification to run a specific harness integration as an agent (or the admin) in
  a workspace on a VM.** The session template names an integration and supplies its
  `harness_integration_config`: an **inline reference-plus-blob** on the consuming resource (the
  third capability hosting shape after a dedicated kind and a keyed map, `capabilities/README.md`
  "Hosting shapes"). An undeclared integration resolves to a plain `shell` login shell, so the
  default session is unchanged.
- **The session node HOLDS the integration instance and composes it (the rich case).** The session
  node's `preflight` / `runup` fan into the integration's, and the integration's declared secrets
  fold into the node's `secret_refs`. The integration owns the launch-target readiness that
  `RequiredCommandsCheck` owned: the skip / defer / probe / loud-error fork keyed on the operation
  scope's LEVEL, the required-commands probe, and a new SESSION-level identity guard (the
  integration raises if handed a context assembled for a different session). The one-object target
  contract carries over: the same agent node is both the session's dependency edge and the
  integration's `target`, so a `mark_realized` flip is observed without rewiring.
- **`start` / `resume` are the integration ops.** They return the raw pane command string; the
  service layer assembles an op-start `RunContext` (execution targets, scoped secrets) at the call
  site and wraps the returned string with the existing `{{session_name}}` / `{{workspace_name}}`
  template-var substitution (lifted out of the deleted `_build_session_command`). `resume` is
  assembled after the old process is killed, so a state-aware integration (claude-code) decides
  resume-vs-launch with it dead.
- **An integration gets a per-session state blob, persisted on the session row.** This reverses the
  SDD's original "database unchanged" assumption, deliberately: the `sessions` table gains an opaque
  `harness_integration_state` JSON column that the manager round-trips (reads it into the
  integration constructor, writes `harness_integration.state` back after each op), and an
  integration reads and mutates it in place through `self._state`. `shell` uses none of it;
  `claude-code` is the first user, minting and storing a Claude session id there on the first
  `start` so it survives to `resume`. `claude-code` then decides resume-vs-launch with an op-time,
  file-presence probe: it checks whether that stored session id's transcript exists on the launch
  target (a slug-independent `find` under the Claude config dir), empirically confirmed to equal
  Claude's own resume boundary, rather than trusting any in-memory or derived state.
- **`harness_integration` + `harness_integration_config` inherit as a PAIR.** Template inheritance
  merges the pair through one rule (`_merge_pair`): a child silent about `harness_integration`
  leaves the accumulated pair untouched (so an integration-silent later parent no longer wipes an
  earlier parent's config); a child naming a DIFFERENT integration starts from a fresh blob (the
  parent's config was addressed to the wrong capability and never leaks); a child naming the SAME
  integration merges via that integration's `merge_config` (child-wins per key, `shell` unioning
  `required_commands`). Validation runs once on the merged blob, at load: the declarative-schema
  effort (FR12) moved it there from resolve, since a template's merged config is exactly as
  checkable at load as the config of a resource that inherits nothing, and an error naming a key an
  ancestor declared says which template to go and edit.

## Consequences

### Positive

- A session's runtime is a first-class, extensible model concept. Adding a runtime is registering a
  integration class; no session-manager surgery, and the registry lists it uniformly.
- The integration reuses the whole capability contract: shape validation at load on the merged blob,
  the declared-secret / scoped-delivery model, and the preflight/runup split. A future
  secret-declaring integration needs no new plumbing (the node already folds `secret_refs`).
- One readiness fork, in one place, shared by every integration member, replacing the session-only
  `RequiredCommandsCheck` stand-in. The identity guard closes a gap the stand-in never had.
- Pair-inheritance fixes a real multi-parent divergence: an integration-silent parent can no longer
  erase a sibling parent's command.

### Negative

- Inheritance for the workload is now pair-scoped, not per-field, which is a different mental model
  from the other three flat-field template families; the reference-plus-blob shape and the pair rule
  are documented to compensate.
- The migration landed in phases: between the orchestrator swap and the template's surface change,
  the harness integration was always `shell`, built from the template's still-flat fields via an
  interim adapter. Each interim `main` state was complete and honest (the mechanism was real and
  swapped in; only the template selector was pending), but it was a two-step where a one-step
  surface change would have been simpler, deliberately, to isolate the orchestrator wiring from the
  dataclass reshape. `SessionTemplate` now carries only `harness_integration` /
  `harness_integration_config`; the legacy flat `command` / `restart_command` / `required_commands`
  spelling is accepted solely as TOML-loader backward compatibility (hoisted into
  `harness_integration_config` at load) and rejected in manifests.
- The per-session `harness_integration_state` blob is a real, if narrow, expansion of persisted
  surface: the core DB now carries an opaque-to-it, integration-owned blob it never inspects, a
  deliberate reversal of the SDD's original "database unchanged" assumption once `claude-code`'s
  resume-vs-launch decision needed somewhere durable to keep its session id.

## Alternatives Considered

- **Keep the flat fields; special-case Claude Code in the session manager.** Rejected: it hard-codes
  one runtime into core session logic, offers no path for a third, and duplicates the readiness and
  config machinery the capability model already owns.
- **A dedicated `harness-integration` declarable resource the template references by name (like
  `vm-site`), rather than an inline blob.** Rejected for the common case: a session's integration
  config is per-session and small, so a dedicated declarable per configuration would be ceremony;
  the inline reference-plus-blob keeps the simple case a single template, and `validate_config`'s
  host-agnostic `owner` already serves the inline host.
- **Per-field inheritance for `harness_integration_config` (merge each key independently across
  parents).** Rejected: a config blob is addressed to a specific integration, so merging keys across
  an integration switch would leak a parent's `claude-code` flags onto a `shell` child. The pair is
  the correct merge unit.
