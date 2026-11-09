# Artifact delivery reassessment

2026-11-08. Proposal responding to the operator's request to reconsider delivery across all four
artifact types, avoid generated `AGENTS.md` content, and seriously consider generated guidance
skills. The operator endorsed that direction and asked to refine per-rule granularity and consider
ordinary-home session skills and workspaces reserved for one session. Session visibility policy
remains under discussion; this response does not claim that new carriers ship or that the FRD has
been amended. Current main: `f78ffb3f`. Artifact and session implementation is unchanged from the
source-review baseline `89bb63e4`, including merged PR #862.

## Recommendation

Make separately owned files the ordinary delivery model. Use native rule files where a harness
provides a suitable location. Where its instruction mechanism requires modifying a shared document,
provide one generated skill per rule and one aggregate hints skill per applicable original owner
instead. Preserve imported skills as standard skill packages and personas as native agent
definitions. For session delivery, prefer one private native bundle and a path-based loader over
embedding artifact bodies in launch arguments.

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

| Artifact type | Meaning                                                            | Preferred projection                                                                                     |
| ------------- | ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| Rule          | Standing guidance intended to apply throughout work in its scope   | One native rule file or generated required-read skill per rule                                           |
| Hint          | Small setup facts and guidance about the available environment     | One separate aggregate, using a native rule-like file or a generated hints skill                         |
| Skill         | Reusable capability with its complete supporting content           | Standard Agent Skills package, retaining text, binary members and executable intent                      |
| Agent         | Agent persona definition available for native selection/delegation | Native agent file/registration; keep primary-agent selection separate from making a definition available |

The core retains four typed maps, current capture/source semantics, intentional same-scope
overrides, original-owner groups and inward routing. A generated skill is an integration's
representation of a rule or an aggregate of hints; it does not turn those source declarations into
skill artifacts or merge their keys with producer-authored skill keys.

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
return an explained unhandled outcome unless the operator explicitly enables a documented fallback
with weaker native visibility. The ordinary-home candidate below is such a fallback, not private
catalog delivery.

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
- Give generated helpers distinct native names by original owning scope, artifact type and key.
  Preserve original-scope labels inside each hints aggregate. Different originals must coexist even
  when both reach the session facet. Generated helpers participate in the existing native identity
  checks against authored skills; prefixes do not reserve names. Private plugin namespaces must also
  avoid shadowing other installed plugins. This is native output naming, not per-producer namespaces
  in the core maps.
- Reuse ordinary applied-config ownership, fingerprints, package boundaries, partial-progress
  reporting and safe removal. Do not add a second receipt, provenance or model-read tracking engine.
- Keep private session files in the actual user's existing UUID/run directory. Public catalog
  availability and Git exclusion do not create filesystem isolation.
- On resume, make the current files/catalog available through the selected loader and ask the agent
  to consult them. Previously read text can remain in conversation history; neither a restart nor a
  skill description magically rewrites that history. Verify native refresh and explicit activation
  of changed content before claiming those behaviors.

## Granularity: one rule, one file or skill

Preserve each rule as its own native file or generated skill. Its existing optional description can
inform the generated description; add a short standing-guidance instruction and original-owner
label. When the source has no description, use a deterministic description from its name and scope,
not a model-generated summary. Keep the rule body intact. Updating or removing one rule then changes
only that rule's output, and inspection can identify it directly.

Hints are intentionally small setup facts. Aggregate them once per original owner into a hints file
or skill, with entries labeled inside. Do not combine distinct original owners merely because their
hints arrive at the same facet. This follows existing scope separation rather than adding an output
namespace for every producing resource.

## Session skills in the ordinary home: considered fallback

The operator's idea is viable as an explicit semantic applicability convention. A native user skill
can have a unique session-qualified name and a description saying to use it only for the matching
managed session/run. Other sessions under that user may discover the entry; the model decides
whether it applies. That additional decision is a real tradeoff, but it is not fundamentally
invalidated by the fact that prose adherence is voluntary. Even automatically inserted rules rely on
model cooperation.

Prefer a verified invocation-local loader where one exists. For an integration without one, consider
an opt-in ordinary-home workaround for guidance and skills, with the following concrete limits:

- Put the session/run condition first in the description and repeat it before the body. This is an
  instruction, not a native visibility or invocation barrier. Shared catalogs can expose names,
  descriptions and paths and can become crowded; no claim of private catalog delivery is made.
- Use the durable Agentworks session UUID and fresh run identity, not the reusable display name or
  native conversation ID, for ownership and applicability. Workloads already receive private
  UUID/run environment fields, but implementation must define and document the supported selector
  rather than quietly making those private recovery fields a public contract. Ordinary child
  processes inherit the parent identity; native delegate behavior needs verification before making a
  stronger claim.
- Give each prospective run disjoint native package paths/names. Stage without replacing the old
  run's live files; retire old packages only after safe replacement, and clean only recorded
  unchanged outputs. Removal or restart of one session must never modify another session's package.
  Reuse existing applied config and publication ownership; no shared mutable current-session
  pointer.
- Keep each rule separate and aggregate hints per original owner. Retain skill support files and
  relative paths. Session qualification changes native invocation names; do not rewrite arbitrary
  skill bodies or pretend cross-skill references automatically follow a rename. Those limitations
  must be handled or disclosed for imported skills.
- Use native selectors only where their per-process behavior and preservation of unrelated operator
  controls are verified. Do not edit shared enable/disable defaults on every start, toggle them
  temporarily, replace the entire native home, or rebuild unrelated configuration arrays just to
  simulate session visibility.
- Do not automatically extend the skill fallback to native agent personas. Providing readable
  persona content is distinct from making a definition available through native delegation or
  main-agent selection. Keep suitable native registration, or leave that input explained as
  unhandled.

Claude documents both a private plugin loader and per-skill visibility overrides through
session-only settings. The private loader is the simpler first choice. Codex has discovered-skill
enable/disable selectors, but the documentation does not establish an additive CLI override that
preserves its existing selector array. Grok documents disabled-name lists without an established
selective per-invocation override. Names/descriptions remain the portable convention, not a
substitute claim that all these native controls are verified.
[Claude skills](https://code.claude.com/docs/en/skills),
[Claude session settings](https://code.claude.com/docs/en/settings),
[Codex skills](https://learn.chatgpt.com/docs/build-skills),
[Grok settings](https://docs.x.ai/build/settings/reference)

Current session validation, staging and cleanup allow only the private UUID/run subtree. The home
fallback therefore requires a bounded lifecycle change, not just a description edit or a blanket
home-directory allowance. Its native package roots must be narrowly authorized and independently
checked on publication and cleanup. Test two concurrent sessions, an unsuccessful replacement,
restart, deletion, name reuse and preserved operator files/configuration.

Retain native private loading as the first choice. Ordinary-home skills remain a viable named opt-in
when native private loading is unavailable, provided the shared-catalog tradeoff is accepted. This
qualifies the current session-boundary requirement; it must not silently be recorded as proof of
private native delivery. Per-rule granularity increases catalog entries, which is a practical reason
to avoid the home fallback where a private loader works. Do not add a generic shared dispatcher
merely to reduce that count: it creates shared ownership and lifecycle questions, and a second layer
the agent must read, without establishing native persona registration.

## Single-session and multi-session workspaces

The operator selected two workspace modes. A single-session workspace admits at most one registered
logical session, including a stopped session. Its reservation lasts until that session is deleted; a
replacement can then be created. A multi-session workspace retains the current ability to host
multiple independent logical sessions. Keep these names explicit: filesystem grants can be shared in
either mode, so naming the modes private/shared would confuse access with session admission.

Use the durable session UUID for the current reservation, not a reusable name or an active-runtime
lease. Existing start/restart can switch the effective harness through configuration while
preserving the logical UUID and each integration's separate native conversation state. Deleting the
old session and creating a new one is also an intentional handoff, with ordinary cleanup between
owners. This does not translate conversation history between harnesses.

Single-session mode supplies a provisioning invariant for native project placement of session
artifacts. Workspace and session declarations retain separate original scopes, native identities,
applied state and cleanup, even when both use project directories. Session output can refresh with
its own lifecycle without adding general workspace reinit. Reconcile recorded session-owned files
when its artifacts change or the session is deleted; preserve workspace-owned and repository content
and use existing fingerprints, package boundaries, partial-progress reporting and cleanup rules. Do
not sweep native project directories or add a second tracking mechanism.

Admission belongs to workspace lifecycle code, checked on every managed create/start/restart path,
including batch entry points, with creation rechecked at the existing mutation boundary. A stopped
owner still occupies the slot. Deletion releases it for a new registered session under ordinary
lifecycle and cleanup policy. The baseline has no mode or admission check; observing one session or
no other running session cannot substitute for this implemented invariant. Filesystem grants remain
independent, and granted users can still read the project files.

Prefer native project placement for session inputs in this mode where the integration supports it.
Native invocation-local loading remains appropriate for multi-session workspaces or where it better
fits the artifact. Git exclusions, native discovery, trust, collisions and failed-start behavior
still need verification; single-session mode does not make Grok load ignored rule files. For
multi-session workspaces lacking an invocation-local loader, keep optional unsupported inputs
explained as unhandled rather than building the home fallback speculatively.

The operator selected single-session as the default for new workspaces and preserving existing
workspaces as multi-session. The operator also explicitly separated this into an additional PR:
current artifact delivery must solve multi-session workspaces now on a best-effort basis. Do not add
workspace modes or rely on their invariant in the current PR. Native invocation-local loading
remains the first choice, with the documented ordinary-home session skill workaround for
integrations that lack a suitable native loader. The later workspace mode/admission and placement
work must be designed and implemented together before relying on exclusivity.

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
