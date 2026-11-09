# Native artifact delivery LLD

**Status:** implementation detail of the approved [HLA](hla.md). The effort lead owns publication,
applied state, routing and session lifecycle. This document specifies the first-party adapters'
native plans and acceptance boundaries.

## Approved correction: native files and best-effort multi-session delivery

The 2026-11-08 operator ruling in the FRD supersedes predecessor prompt-injection and shared-section
choices. The detailed adapter plans below follow that correction. This PR supports today's
multi-session workspaces; workspace modes are a separate PR.

- Preserve one native file or generated skill per rule and one hints aggregate per original owner.
  Use generated guidance skills when dedicated native rule files are unavailable or incompatible
  with keeping generated workspace files out of commits. Do not mutate shared AGENTS documents.
- Claude uses one private session plugin for enabled guidance, skills and personas. Guidance becomes
  skills through `session-skill-plugin`; `session-agent-definitions` selects native plugin personas.
  Retire `session-prompt` and its version/snapshot probing rather than keeping parallel carriers.
- Codex uses `session-home-skills` for rules, hints and skills, with bounded names qualified by
  original owner and session/run. The description and body identify the intended managed workload;
  catalog metadata is visible to other sessions of that Linux user. Codex retains private
  `session-agent-config` for personas.
- Grok uses the same opt-in name but one stable session-owned entry skill because genuine native
  resume retains its initial catalog. Its immutable metadata names the logical session UUID, while
  its body reads the current `AGENTWORKS_ARTIFACTS_DIR/index.json` at use time. Current rule files,
  per-owner hints aggregates and complete canonical skill packages live in the private run
  directory. Reuse the existing filesystem renderer/index shape. Indexed skills are not individually
  natively registered; unsupported native activation controls are unhandled, not emulated. Grok
  agents remain unhandled. Explain the first-enable limitation for native conversations predating
  this entry.

- Keep canonical declarations/captures unchanged. Preserve complete skill assets and executable
  intent. Rendering can qualify native names; disclose invocation-name changes and do not rewrite
  arbitrary skill body references. Never claim that a description guarantees loading or adherence.
- Export supported public managed UUID/run variables alongside existing private recovery fields,
  with identical values. The durable UUID identifies the session; fresh run identity selects its
  current private files. Do not substitute a reusable display name or native conversation ID.
- Extend session publication only for narrowly recognized session-owned native skill packages
  beneath the actual user's home. Derive ownership from the session UUID; qualify changing packages
  by run, while the immutable Grok entry stays stable. Enforce bounded native naming. Existing
  package_root, native_identity, fingerprints and applied state suffice to track them. Avoid a
  separate receipt, root registry or global current-session pointer. Validate desired and persisted
  placement before publication or cleanup, retain old live run files while staging, and retire only
  unchanged recorded old packages after teardown.
- Keep other session files in the private run subtree. A broad HOME allowance is not authorized.
  Core validates every desired and persisted home member against its exact UUID-affine skill
  package. The generic publisher retains its strict package-root-below-boundary invariant: native
  skills parent directories are boundaries only after that exact validation; cleanup prunes only the
  recorded packages. Stage/replace/delete all use this two-level check.
- Retire only recorded Agentworks sections from old instruction documents, including their markers,
  preserving all surrounding bytes. Do not delete an otherwise empty shared file without whole-file
  ownership. Preserve malformed markers with diagnostics and retain ordinary cleanup evidence.
- Locally exclude exact confirmed owned workspace paths through Git-resolved metadata, preserving
  unrelated exclusions and tracked files. Reuse ordinary applied state for exclusion ownership; do
  not hide whole native directories or use index flags. Grok skips ignored rule files, so use a
  verified skill representation or diagnose unsupported delivery instead of bypassing discovery
  checks.

The only new session workaround name is `session-home-skills` for Codex/Grok. Remove replaced prompt
workaround spellings and update schema/help/examples/guides together, documenting the config change.
Do not reconstruct user selector arrays, toggle shared native configuration during launch, or add a
new native-version framework. Native file/catalog/current-content evidence is distinct from model
obedience and old text remaining in a resumed conversation.

## Integration boundary

Every first-party integration implements all four facets at contract version 7. VM facets publish
supported artifacts directly and defer unsupported types to user handling, without inspecting
downstream activation. Shell publishes all types under `/opt/agentworks/artifacts`. Claude uses its
managed memory, skill and agent locations; Codex uses its machine skill directory. Grok defers VM
inputs to user handling until machine discovery registration can preserve configuration and trust.

Outer hooks return `ArtifactApplication` after their existing native settings/plugin setup. The
integration selects the exact file bytes, paths, executable intent and native identities. Core
publishes those files and retains ownership in the existing applied-state slice. Retirement returns
an empty plan, letting core remove previously owned artifact files.

Native harness session delivery is disabled by default. Shell publishes its session files directly.
An enabled native workaround returns the same application with its native command; disabled inputs
return terminal session deferrals with actionable reasons. The optional `artifacts_dir` field must
identify the core-provided private run directory; core exposes it as `AGENTWORKS_ARTIFACTS_DIR` in
the launch environment. Outer facets cannot set this field. All artifact argv values use
`quote_literal_argv`, including JSON and TOML values, so core command-template substitution cannot
interpret literal `{{...}}` in artifact content. Initial prompts keep their existing
fresh-conversation behavior. Artifact files and catalogs are prepared for each managed launch,
including resume. Already read text can remain in conversation history; current file availability is
not proof that the model has reread it.

The adapters do not call one another, fetch sources or modify captured inputs. The modules under
`artifacts/native/` are rendering/probe utilities used by integrations. Core routing does not import
a plugin or select a native format.

Inputs retain per-type maps in one local owner group and separate deferred groups identified by
original owner. An adapter may iterate groups, but it must not collapse equal keys from different
owners into one name map. Within-owner replacement is already resolved by core composition. Native
rendering must preserve every surviving contribution or explicitly refuse the combination.

## Files and native identities

| Integration | User and workspace files                                                             | Session carrier                                                                                         |
| ----------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| Shell       | Grouped typed files and index under user/workspace artifact directories              | Same layout in private run directory                                                                    |
| Claude Code | Separate native rule files, complete skills and native agent files                   | One private plugin containing generated rule/hint skills, full skills and selected native persona files |
| Codex       | Individual rule skills, owner hints skills, complete skills and native persona TOML  | Session-qualified ordinary-home skill packages; private persona role files with native config selectors |
| Grok Build  | Native user rules; compatible project guidance skills, full skills and native agents | Stable session-owned home entry reading current private files/index; session personas unsupported       |

User native roots honor `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `GROK_HOME`. Codex's standard user
skills remain under the actual user's `.agents/skills`, independently of `CODEX_HOME`. Workspace
placement uses the workspace root and does not consult an admin's home or native installation as a
substitute for the future session user.

Shell preserves each original owner under `scopes/<component>/<resource-name>/`, with separate
`hints`, `rules`, `skills` and `agents` directories. Its index retains those owner groups and type
maps, so equal keys from different owners resolve to different files. Shell publishes content for
filesystem consumers; it does not load assistant context.

VM publication uses the existing admin transport with privileged destination operations, not an
admin-user placement. Files are root-owned and readable by VM users; newly created directories are
traversable. Destination selection remains integration-owned within one global core allowlist:
`/etc/claude-code`, `/etc/codex` and `/opt/agentworks/artifacts`. `NativeFiles` checks elevated
reads, writes, directory operations and cleanup before dispatch or payload staging. Publication also
checks all desired and persisted destinations and package boundaries before opening staging. The
existing destination-side checks reject symlink traversal. This is a file-helper restriction, not a
sandbox for in-process integration code. The helper is the replacement seam for the proposed
Transport file APIs in PR 795; this correction adds no new execution interface or per-integration
grants. VM deletion still removes the filesystem without artifact cleanup becoming a prerequisite.

Codex rules and hints use separately owned generated skills, rather than modifying AGENTS documents.
Claude uses separate native rule files where supported and generated machine guidance skills where a
suitable native machine location exists; unsupported types defer to user. Each rule retains one
output. Hints aggregate per original owner; equal keys from different original scopes retain
distinct native identities. Generated helper names are bounded and use the same collision checks as
imported skills.

Generated guidance skills have two ordinary owned package members: a fixed navigation `SKILL.md` and
sibling `guidance.md` containing the literal rule or owner hints aggregate. Keep freeform guidance
and source descriptions out of the native preprocessing entrypoint. Include a deterministic content
digest in its body so changed guidance changes the rendered entrypoint too. Session applicability
stays in the wrapper; canonical types, captures and origin identities do not change. Do not escape
selected syntax, run a shell loader or grant tools. Authored skills retain their native body
processing and complete supporting files.

Retire old recorded instruction sections by removing only Agentworks content and paired markers,
preserving surrounding bytes, ownership, permissions and extended attributes. Malformed markers warn
and retain cleanup evidence. Do not infer ownership of an entire shared file merely because it
becomes empty. Ordinary skill/package replacement still requires matching recorded bytes. The
complete correctly delimited interior is managed, including any later operator edits there.

Skill support files and executable intent are preserved. The Claude private plugin contains only
selected skill and agent definitions plus its manifest, with no hook or MCP configuration. Its
namespace is session/run qualified to avoid replacing another installed plugin. Codex home packages
use bounded session/run-qualified names with exact applicability conditions. Grok's stable entry
instead has immutable logical-session metadata and reads the current private bundle/index through
the process environment. Its canonical skill packages and relative assets remain unchanged, but are
accessed through the index rather than individual native skill commands. Catalogs remain shared;
neither metadata nor successful file publication proves model adherence.

Core accepts outside-private session files only in narrowly recognized skill packages beneath the
actual user's home. Existing package_root and native_identity identify each package, and its
namespace must belong to the managed session UUID. Codex native packages and all private run files
are disjoint across runs. Grok's immutable native entry belongs to one logical session and can be
reused through guarded identical-byte publication; old/new process environments select different
private directories. Deduplicate retained/new ownership by exact path. Commit retains the current
desired application paths held in memory, not an added persisted registry.

Core validates exact UUID-affine package membership before passing native skills parent directories
to the strict generic publisher. HOME is never a publication boundary; cleanup prunes only recorded
exact packages. Retire unchanged old files after teardown. Display-name reuse cannot acquire another
UUID's ownership. Public AGENTWORKS_SESSION_UUID and AGENTWORKS_RUN_ID expose the existing managed
identity values alongside private recovery fields. Grok's entry reads AGENTWORKS_ARTIFACTS_DIR at
use time instead of embedding the previous run's resolved path.

Native names are checked across local/deferred owner groups and persisted ancestor ownership
metadata, with supported native discovery locations checked for existing entries that can shadow
managed delivery. A user skill and a workspace skill named `foobar` remain distinct obligations even
if the native harness normally chooses one by precedence. Same-scope composition never authorizes
that cross-scope suppression. Outer setup uses the owners it knows; the session checks the joined
actual user/workspace context without retroactively running ancestor setup. Multiple members
belonging to one skill are one identity. Distinct origins claiming the same native skill or agent
identity cannot be delivered by silently choosing one. Setup defers affected native entries with
unchanged origins and reports partial completion; unrelated entries continue. Previously owned
placements omitted from the new plan follow ordinary safe retirement, while modified/unowned content
is retained. Native identity collisions leave affected session inputs unhandled with a report;
independent inputs continue. Conflicts solely between existing ancestor placements are reports.
Unsafe filesystem destination overlaps remain errors. Supported session persona definitions are
retained as individually owned files with their native identity; Claude uses native Markdown persona
files in the same private plugin. Grok session personas remain unhandled, with no generated files or
arguments. Previously handled ancestor payloads are not retransmitted.

Hints aggregate into one rule-like file or generated skill per original owner, using a qualified
hints name. The native representation must not lose another scope's aggregate to filename or
identity precedence. Neither the file name nor successful publication alone proves that two
same-named files are both discoverable; the adapter's native contract must establish that or select
another faithful representation.

Native identity is type-specific. Claude documents additive rules but name-based skill and persona
precedence; its plugin skills have a separate native namespace. Codex documents that same-named
skills can both appear in selectors. These facts do not establish a universal basename collision
rule. Adapters preserve additive rule/hint bodies and check skill/persona identities independently.
See [Claude layering](https://code.claude.com/docs/en/features-overview),
[Claude skills](https://code.claude.com/docs/en/skills), and
[Codex skill discovery](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills).

## Agent personas

The portable persona has a name, description and instruction body. Native options are supplied under
`native_options.<integration-name>` in the captured persona. Each adapter validates its own options
and rejects unsupported fields rather than silently losing their requested behavior.

| Integration | Accepted native options                                                                 | Encoding                                                                                                                                                     |
| ----------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Claude Code | `model` string; `tools` and `disallowedTools` string lists; `maxTurns` positive integer | Markdown frontmatter for outer facets and private session plugins                                                                                            |
| Codex       | `model` and `model_reasoning_effort` strings                                            | Native discovery TOML includes name/description; session role config layers contain only config fields, with identity/description in the selecting overrides |
| Grok Build  | `model` string and `tools` string list                                                  | Markdown frontmatter at user/workspace facets; session personas unsupported                                                                                  |

Codex CLI overrides use literal dotted key components, not TOML key syntax. Session role selectors
therefore use the validated portable persona name directly in `agents.<name>.config_file` and
`agents.<name>.description`; only their values are TOML-encoded. Unsupported programmatic names are
rejected before publication rather than creating another nested key or a name containing quotes.

Hooks, MCP definitions and unknown option fields are not accepted through this surface. Registering
a persona makes its native definition available; it does not silently select the primary persona.
Existing integration `agent` configuration retains its documented behavior even when its same-named
managed definition is left unhandled; the native tool can select an existing native definition. The
warning does not claim managed-definition delivery. In particular, Codex primary-persona selection
remains prompt-mediated and does not promise that the persona's model or other runtime configuration
applies to the primary thread.

## Native preflight

Preflight resolves the native root in the actual login environment. Declared environment is passed
through the transport's environment channel, not embedded into logged command text. A changed actual
`HOME` is an error because session publication belongs to the Linux user selected by core. A changed
native home that no longer discovers an applied ancestor's files is reported with its owning repair
operation and does not block session launch. User artifact publication honors native-home overrides
beneath that user's `HOME`. A plan that would publish outside `HOME` is explicitly unsupported and
fails before settings or plugin mutations. Persisted ownership and retirement therefore share the
same user boundary. A session can still use an external native home where its selected carrier stays
in the private run directory; the home-skill workaround requires native discovery beneath HOME.
Existing ancestor-only discovery problems are warnings; new session delivery must still validate its
own carriers, destinations and native identities.

The shared session probe checks new carriers strictly, then checks existing ancestor files through
the advisory path. A mixed-delivery start therefore retains ancestor warnings without adding old
paths to the strict request. Integrations keep their native discovery roots and carrier details.

Codex/Grok guidance is represented by ordinary native skills, not injected through launch prose.
Claude's plugin and Codex's private role configuration use path-based native selection. Native
conversation history can retain older text; descriptors instruct the matching workload to consult
current guidance without claiming automatic history replacement or guaranteed obedience.

The native checks use selected configuration facts and a bounded inventory of relevant skill/persona
entrypoints; no model is launched. Inventory compares actual metadata names and paths against
planned files and already handled ancestor files. YAML headers are parsed on the workstation with
the same alias, size and depth guards used during capture, so the guest needs no YAML dependency;
Codex TOML and configuration-registered role names use the standard library. Publication writes
skill entrypoints before supporting members, so an interrupted large-package write remains
inspectable and retryable. Retirement reverses that order: `SKILL.md` stays present and owned while
any owned supporting member or required parent cleanup remains. Modified or unowned files remain
untouched. A modified obsolete member of a still-requested skill skips the whole package before
publication, so new instructions cannot be combined with that retained member. A differing whole
file in a prospective session directory refuses staging before the old runtime is torn down; setup's
per-entry skip policy does not authorize launching with that content.

Retirement prunes empty parents of each removed owned skill member only within the exact package
root supplied by the integration and retained on that file's existing record. Core validates that
the root contains the file and lies within its owning publication scope. Failed inner-parent cleanup
retains the member record for retry, even if its leaf file is already absent; diagnostics identify
the package and permission repair. Dropping that evidence could leave a large directory tree without
an entrypoint and make the next bounded inventory refuse it. Permission denial when removing only
the final empty package root is different: retain that single empty directory, warn and checkpoint
the completed retirement. Unsafe paths and ambiguous transport failures remain errors.

An integration may omit the optional package root. Such records still permit owned-file retirement
but never directory pruning. A recorded native skill identity and `SKILL.md` basename identify the
entrypoint for ordering only; deeper entrypoints retire before shallower ones. This supports the
current plugin contract, not only historical records. In directories without a root `SKILL.md`,
bounded inventory ignores ordinary files and empty directories while refusing nested skill
entrypoints, symlinks and special files. Unowned notes therefore survive cleanup without keeping an
operator-removed skill discoverable.

Inventory covers the selected types at known user/workspace discovery roots, with at most 512
inventory entries, 32 KiB per YAML header and 1 MiB of headers in total.
[Codex recursively discovers skills beneath those roots](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/ext/skills/src/loader/discovery.rs),
so its check also counts package members and refuses `SKILL.md` outside the supported
`<root>/<package>/SKILL.md` placement, even when an enclosing package has its own entrypoint.
Proposed supporting files and implied directories count toward the same bound before publication.
This is a Codex adapter limit, not a restriction on captured packages for other integrations. Codex
persona TOML is bounded by Agentworks at 32 MiB per file and 64 MiB in total, including the
instruction body; these are our rendered-content budgets, not Codex product limits; generated
personas obey the same per-file limit. Preflight budgets existing and planned native entries
together, counting each replacement path once, so publication cannot itself exceed the next
inventory's limits. Symlinked paths and nested candidate layouts are conservatively refused.
Additional repository ancestor roots, third-party plugin discovery and changes after preflight need
separate verification. These are Agentworks support limits, not assertions that other layouts are
invalid in the native harness, and the check is not a continuous watcher or concurrent-mutation
guarantee.

There is no blanket native-version minimum for artifact delivery. Tested versions in the evidence
section record observations only. The probe does not certify carrier support by searching `--help`:
Claude explicitly documents that help omits supported flags. An opt-in forwards the documented
carrier to the native CLI, which owns acceptance of its configuration.

Settings checks concern the selected native identities. An unrelated disabled plugin or skill does
not require enabling it. Claude rule exclusions are checked against requested rule paths; extended
exclusion patterns that this adapter cannot interpret are diagnosed explicitly. Codex skill
selectors and Grok disabled skill names are checked against supplied artifacts. Grok workspace paths
are checked against Git ignore rules; user files outside that workspace are not fed to the
workspace's ignore check. A relevant restriction on newly requested delivery in an inspected
configuration layer causes a conservative refusal. An ancestor-only restriction is a report and does
not prevent launch. The adapter does not reconstruct native trust or merge precedence, so a later
layer that reenables an artifact does not clear that refusal. Diagnostics identify a configured
restriction without claiming it is the native effective policy.

User root resolution precedes rendering. Policy checks follow existing settings application, so
reinitialization can remove an obsolete exclusion and then publish artifacts. Workspace checks
inspect project policy without assuming the admin's native user setup. Session checks include
ownership metadata for handled ancestors as well as the prospective run's files.

`enabled_workarounds` remains a default-empty replacing list. Claude accepts `session-skill-plugin`
for guidance/skills and `session-agent-definitions` for personas; both use one private plugin. Codex
accepts `session-home-skills` and its separate `session-agent-config`. Grok accepts
`session-home-skills`; personas remain unhandled. Shell requires no workaround. Remove replaced
prompt spellings and document the migration. Artifact delivery performs no snapshot/version probing,
shared-selector toggling, selector-array reconstruction or implicit user-facet activation.

Native private loading is preferred where available. Ordinary-home catalogs expose metadata to other
sessions sharing that Linux user. Codex session/run descriptors and Grok's stable logical-session
entry express best-effort applicability. Grok resumes created before first enabling the entry may
need a fresh conversation or manual read of the current index; no read-attestation state is added. A
related disabled skill or policy restriction still receives an honest diagnosis. No claim is made
that descriptions enforce visibility or model behavior.

Native-name conflicts in enabled session delivery leave the identified inputs terminally unhandled.
The shared session pipeline rerenders complete files and arguments for the remaining inputs and
repeats validation; removing one proposal can expose another collision with retained native content.
A supported namespace can resolve an earlier setup collision. Malformed inputs, invalid native
responses, transport failures and unsafe filesystem publication remain hard errors. Raw argument
override conflicts are still checked for enabled new session delivery. Ancestor-only discovery
problems are reported rather than enforced as session prerequisites. Disabled session inputs do not
introduce restrictions on native launch arguments. Outer native placement and its safety/ownership
checks remain in place. Core emits final unhandled warnings after full output validation and stores
the deferrals so inspection does not claim success. Setup-facet domain and transport failures retain
their sanitized reason and focused remediation in incomplete state and the command's failure list.
Independent setup and owner bookkeeping continue before the terminal error summary; these failures
are not recast as unexplained SSH failures. Persistence failures and cancellation still abort.
Session preparation errors retain their existing fail-before-teardown behavior.

An ancestor-only check still returns its native root for discovery-path diagnostics. A missing
ancestor-only target produces an advisory warning; a missing target needed for new delivery remains
an error. Mixed new/ancestor delivery uses a separate read-only advisory query, preserving strict
checks over new paths without turning ancestor policy into a launch prerequisite.

Native argv and the fully quoted returned command each have a conservative 32 KiB UTF-8 limit,
leaving room for launcher wrapping below the operating system's per-argument limit. Oversized text
fails during preparation with guidance to reduce it or use a suitable native file carrier. Guidance
and skill bodies are file content and do not consume that argv budget.

## Git exclusion scope

Exclusion claims own exact marker/pattern associations, not the repository's other metadata or
worktree contents. Resolve repository-relative patterns from Git's prefix and literal
workspace-relative file names, including nested workspace directories. Do not compare Git's physical
root spelling with a lexical configured path. Only the metadata destination independently resolved
by current Git authorizes I/O. After a Git-directory move, matching owned pairs there can converge
without following old persisted destinations. Invalid associations warn and release; modified pairs
retain their evidence. Existing native file path guards still apply.

Before destructive workspace directory/group/record removal, reconcile known artifact exclusion
claims with no desired files and checkpoint the remaining ordinary native setup state. Failure or
modified pairs retain the workspace and cleanup evidence for retry. Unreadable historical setup
state retains the existing warning/best-effort parent-deletion fallback; non-exclusion claims do not
create another teardown engine. Missing-VM cleanup and whole-VM deletion retain their existing
behavior. Git can share info/exclude across linked working trees; the same untracked relative path
in another worktree is then ignored too. This native repository-level effect is explicitly
disclosed, not presented as per-worktree isolation. Do not introduce another Git settings or
reference-count engine to hide that limitation.

## Evidence and acceptance

Focused tests cover complete skill packages, executable and binary members, native paths and
identities, generated guidance skills, fresh/resume carriers, literal template syntax, explicit
best-effort home packages, ancestor collisions, native home changes and targeted discovery-policy
failures. Target-side probe tests execute the actual probe program against temporary settings, an
isolated Git repository and fake native executables; they do not contact a backend or a model.

The following historical observations explain predecessor choices, not acceptance of the current
file/skill correction. Fresh current evidence must be recorded before closeout.

Read-only local observations on 2026-09-13 found Claude Code 2.1.265 and Codex 0.153.4. Claude's
local help describes the prompt-snapshot behavior and the required session carriers. The official
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference) and
[memory documentation](https://code.claude.com/docs/en/memory) describe the native launch and
unconditional rule mechanisms. Codex's official
[configuration schema](https://developers.openai.com/codex/config-schema.json) defines role config
selection and skill selectors.

Grok's CLI and persona parsing were checked against
[official source at `37949780`](https://github.com/xai-org/grok-build/blob/37949780c144e37df692e3d669051a21fec24f20/crates/codegen/xai-grok-pager/src/headless/cli.rs).
Its inline-agent parser maps the `prompt` field to the persona body and supplies the map key as the
native name. Its [settings reference](https://docs.x.ai/build/settings/reference) documents disabled
skill names and per-subagent toggles. No Grok binary was available locally.

An isolated local Codex 0.153.4 check used user `features.multi_agent = false` and a trusted Git
project with `features.multi_agent = true`. Native `codex features list` reported
`multi_agent=true`; the artifact preflight refused the user-layer restriction. This confirms the
conservative boundary, not native disablement. Blindly merging project settings would be incorrect
when Codex declines to trust that project. Claude settings merge semantics and Grok
managed/requirements overlays also remain outside this probe's effective-policy resolution; native
acceptance must exercise the actual policy environment.

Additional isolated checks on 2026-09-13 exercised the installed binaries with network access
blocked and no model calls. Claude's actual `agents --json` accepted the renderer's rule and hint
carrier flags; the public preflight also accepted both after recognizing the bracketed help
spelling. An unknown flag was rejected by both the native CLI and preflight. These checks establish
carrier acceptance, not prompt consumption. Codex app-server diagnostics discovered generated user
and project skills at their respective scopes, then stopped discovering the project skill after its
file was removed and the list refreshed. This verifies native discovery; it does not establish core
cleanup or two-user isolation.

Further isolated diagnostics exercised actual Codex role-file discovery at user, project and session
paths: valid generated files produced no parse warning, while a malformed-file control at each path
produced a native path-specific warning. A successful `config/read` alone does not establish
effective role availability. The no-prompt `debug prompt-input` command rendered the exact composed
developer instructions, establishing native prompt construction without model consumption.

Claude's native validator accepted the generated private plugin manifest. Its plugin inventory
reported the generated skill twice, and reported no skills after the fixture's `SKILL.md` was
removed. This establishes native plugin discovery and its refresh behavior, not Agentworks cleanup.
Available offline diagnostics did not establish Claude persona registration or actual conversation
resume. These checks used isolated homes, denied native network access and made no model calls.

Live native acceptance remains required before shipping: verify native discovery, registered
personas, rule and skill loading paths, fresh/resume carrier selection, native policy refusal and
cleanup through native diagnostics and a scoped VM. R10 excludes model calls and external
marketplaces from this acceptance. File existence, native diagnostics and green renderer tests must
not be reported as proof that a model consumed an artifact.

## Failed routing and conservative publication

A setup attempt starts incomplete while retaining its prior input IDs and deferrals. A recorded
routing table is confirmed only for the current prepared inputs it covers, including explicit
incoming routes whose payload is unreadable. Content changes create new input identities. An old
non-null table, including an empty table, cannot establish handling for added or changed inputs. A
native-only failure can leave a fully covered routing result usable; completeness alone is not the
routing test. A failed outer facet can still feed readable old deferrals to descendants without
authorizing retirement as though an unknown declaration were empty.

The internal routing result distinguishes known unresolved input IDs from unavailable routing
without IDs. A confirmed routing table with no routes to a receiver does not hold that receiver's
own content merely because unrelated native setup is incomplete. Missing, unavailable or stale
capture evidence cannot authorize retirement, even when an older table has no deferrals. Missing
payload for an explicit applicable route remains unknown even when current bundle selection is
empty. Inactive facets use core's fixed passthrough independently of opaque old active-facet state.

When unknown applicable routes prevent establishing requested inputs, setup publication holds prior
placements against removal or replacement, including generated sections and related packages. Prior
and current local captures identify independently attributable local files, which can still update
or retire; a shared file containing unknown ancestor content remains held. Actual held paths keep
local publication incomplete. Temporary incoming-route health is recomputed lazily and does not
become a permanent local native failure on a fresh workspace. A healed VM that handles all inputs
can clear that warning without workspace recreation; genuinely new workspace placement still needs
application. The receiving record acknowledges known supplied inputs only, while inspection and the
session join retain the separate unknown incoming-route health. No codec or history store is added.

Missing explicit route IDs survive active receivers, including their first application, and inactive
user/workspace passthrough. An integration receives only readable inputs; core appends the missing
IDs to recorded input and onward-deferral metadata after validating that application. This preserves
the source owner's repair reason without inventing content or accepting unknown IDs from integration
output. The session join retains that metadata. Known unavailable IDs become terminal unhandled
metadata; unknown routing without IDs records its owner and repair reason. The session launches with
readable inputs and records incomplete delivery rather than calling the reduced set current. It does
not fabricate missing canonical content or turn these optional inputs into prerequisites. Old
private files remain usable until runtime teardown; afterward normal owned cleanup retires obsolete
runs. Keeping unreferenced old files would not deliver missing content and does not justify
accumulating run history. The existing applied-state fields carry the outcome; there is no new
persisted ledger or artifact wire format.
