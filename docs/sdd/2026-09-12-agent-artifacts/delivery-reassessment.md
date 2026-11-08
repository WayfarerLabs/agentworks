# Artifact delivery reassessment

2026-11-08. Proposal responding to the operator's request to reconsider delivery across all four
artifact types, avoid generated `AGENTS.md` content, and seriously consider generated guidance
skills. This is a design response, not an approved FRD amendment or a claim that new carriers ship.
Baseline implementation: main `89bb63e4`, including merged PR #862.

## Recommendation

Make separately owned files the ordinary delivery model. Use native rule files where a harness
provides a suitable location. Where its instruction mechanism requires modifying a shared document,
provide generated `agentworks-rules` and `agentworks-hints` skills instead. Preserve imported skills
as standard skill packages and personas as native agent definitions. For session delivery, prefer
one private native bundle and a path-based loader over embedding artifact bodies in launch
arguments.

The useful contract is that the integration presents the applicable, current content through the
selected native mechanism at the correct scope. It can prove file publication, discovery and native
registration. It cannot promise that a model will consistently follow prose, including prose already
inserted into context. Claude's own documentation makes that guidance/enforcement distinction.
[Claude memory](https://code.claude.com/docs/en/memory)

A skill description directing the agent to read standing guidance is therefore a legitimate native
delivery choice, not an automatically disqualified approximation. Its extra activation step remains
real and should be described honestly. We should test that the current file is discoverable and can
be loaded, without making universal model obedience an acceptance condition.

## Keep artifact meaning separate from its native representation

| Artifact type | Meaning                                                            | Preferred projection                                                                                               |
| ------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| Rule          | Standing guidance intended to apply throughout work in its scope   | Dedicated native rule file, or a generated required-read guidance skill when that is the suitable native mechanism |
| Hint          | Small setup facts and guidance about the available environment     | One separate aggregate, using a native rule-like file or a generated hints skill                                   |
| Skill         | Reusable capability with its complete supporting content           | Standard Agent Skills package, retaining text, binary members and executable intent                                |
| Agent         | Agent persona definition available for native selection/delegation | Native agent file/registration; keep primary-agent selection separate from making a definition available           |

The core retains four typed maps, current capture/source semantics, intentional same-scope
overrides, original-owner groups and inward routing. A generated skill is an integration's
representation of rules or hints; it does not turn those source declarations into skill artifacts or
merge their keys with producer-authored skill keys.

## Native selection by integration

| Integration | Intended file-oriented approach                                                                                                                                                                                                 | Boundary still to verify                                                                                                             |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| Shell       | Continue owned files and the grouped index at each supported scope                                                                                                                                                              | No native model loading or compliance claim                                                                                          |
| Claude      | Keep separate user/project rule files; use guidance skills where there is no suitable separate rule location. A private session plugin can package skills and personas, including generated guidance skills.                    | Validate combined private bundle, names and current-content loading on resume; do not infer it merely from the public flag           |
| Codex       | Replace generated user/project `AGENTS.md` sections with rules/hints skills. Keep standard skill packages and native persona TOML. Machine skills can handle guidance when that machine location is supported.                  | The existing TUI integration has no verified private skill-root launch mechanism; app-server support does not establish one          |
| Grok        | Keep suitable separate user rule files. Select a file/skill/plugin mechanism for workspace delivery that can coexist with keeping generated files out of commits. Prefer a private bundle for session delivery where supported. | Current docs describe a plugin-path loader, but installed-baseline acceptance and skill/plugin Git-ignore behavior remain unverified |

Prefer one verified bundle loader per integration to independent launch-injection tricks for every
artifact type. Keep session delivery explicit under `enabled_workarounds` while these methods need
that conservative opt-in. Where a native mechanism is unavailable, retain the original scope and
return an explained unhandled outcome; publishing private session content into a global skill
directory is not an acceptable substitute.

Claude documents a session-only plugin loader, and its plugin format includes skills and agents.
Codex documents process-local extra roots in its app-server API; this is distinct from interactive
CLI launch configuration. Grok's current documentation lists a plugin directory option. These facts
identify candidates, not a universal supported baseline.
[Claude plugins](https://code.claude.com/docs/en/plugins/create),
[Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Grok skills/plugins](https://docs.x.ai/build/features/skills-plugins-marketplaces)

A bounded Codex 0.167.0 metadata probe additionally found the exact project skill in native
`skills/list` both before and after an exact repository-local Git exclusion. A parsed
`skills.config` override disabled a known skill but did not discover an outside skill. The native
app-server's `skills/extraRoots/set` added and removed that outside skill in two cycles. These four
native invocations requested no model turns; they prove catalog behavior, not body activation,
resumed-thread refresh, thread isolation or a TUI launch option.

## Scope, names and lifecycle

- Apply at the defining scope where a real native location exists; otherwise use the existing
  deferral mechanism. VM, user, workspace and session ownership remain distinct.
- Give generated helpers distinct native names by applicable facet, such as `agentworks-user-rules`
  and `agentworks-workspace-rules`. Preserve original-scope labels inside an aggregate. Private
  plugin namespaces must also avoid shadowing other installed plugins. This is native output naming,
  not per-producer namespaces in the core maps.
- Reuse ordinary applied-config ownership, fingerprints, package boundaries, partial-progress
  reporting and safe removal. Do not add a second receipt, provenance or model-read tracking engine.
- Keep private session files in the actual user's existing UUID/run directory. Public catalog
  availability and Git exclusion do not create filesystem isolation.
- On resume, make the current files/catalog available through the selected loader and ask the agent
  to consult them. Previously read text can remain in conversation history; neither a restart nor a
  skill description magically rewrites that history. Verify native refresh and explicit activation
  of changed content before claiming those behaviors.

## Workspace Git behavior

Generated deployment files should ordinarily be untracked and excluded locally. Use exact confirmed
owned paths; do not hide all of `.claude`, `.codex`, `.agents` or `.grok`, rewrite a repository's
shared `.gitignore`, untrack source files, or use `skip-worktree` to conceal tracked changes. Git's
repository-local exclude file is intended for local workflow files, but its metadata path must be
resolved through Git and can be shared by linked working trees.
[Git ignore](https://git-scm.com/docs/gitignore)

Loader behavior constrains the choice: Grok explicitly skips ignored rule files. A blanket ignore
writer would break that carrier and the current Agentworks preflight. Use a compatible skill or
explicit bundle location when verified, or report the limitation. Do not quietly dirty tracked
instruction files to preserve an automatic-context ideal.
[Grok project rules](https://docs.x.ai/build/features/project-rules)

Existing tracked files require explicit treatment: an ignore pattern cannot conceal changes inside
them. Newly generated helpers should use their own paths and preserve/refuse conflicting native
content through the existing ownership rules. Tracked-file adoption and local-exclusion ownership
must be decided explicitly before implementation; this proposal does not silently change the
existing matching-content adoption contract.

## Migration and acceptance

Retire only recorded Agentworks content from old instruction sections and preserve unrelated bytes.
Current retirement leaves empty markers, so complete marker cleanup needs a deliberate decision; it
must never reset an instruction file from Git or discard unrelated edits. User/VM owners migrate
through their supported reinit operations. Workspace setup is creation-only: existing workspaces
need an explicit supported migration/recreation path, not hidden workspace setup during a session
restart or a fabricated workspace reinit command.

Keep acceptance focused on the native mechanism: exact current files, correct permissions/scope,
catalog/registration, explicit activation, changed-content behavior across fresh/resumed processes,
collision preservation, Git status and safe update/removal. Retain ordinary malformed-input and
publication failures. Avoid a native-version framework or tests that claim to prove model obedience.

The operator would need to approve replacing the current always-loaded rule definition with standing
guidance delivered through the suitable native mechanism. The support matrix, named opt-ins, guides
and migration details must follow the chosen verified carriers. Current unsupported private cases
must stay explicit. The two SDD locks remain conditional on that accepted contract and actual
implementation/evidence, not on this recommendation alone.
