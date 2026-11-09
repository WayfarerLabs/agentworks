# Agent artifacts

An `artifact-bundle` declares reusable hints, rules, standard
[Agent Skills](https://agentskills.io/specification), and agents (agent personas). An owning
resource selects bundles with `artifacts.bundles`; its core setup captures their contents before
explicitly activated harness integrations handle or defer them.

Use `agw resource explain artifact-bundle` for the declaration schema and
`agw resource sample artifact-bundle` for an editable sample. Use `agw artifact show --help` for
inspection selectors. [The worked example](examples/scoped-artifacts.yaml) demonstrates declaring
sources separately from selecting and handling them.

## Select an owner and a handler

VM, admin, agent, workspace, and session declarations accept the same `artifacts` block. Omission
inherits the selected template's bundle references; an authored list replaces them, and
`bundles: []` suppresses that owner's inherited references. These choices do not suppress another
scope's inputs.

Scopes own resources and lifecycle. A facet is the scoped part of a capability: both the VM admin
and an agent have a `user` facet, each running as its actual Linux user. An agent does not inherit
the admin user's artifacts. A workspace has a VM ancestor, with no implied user ancestor. A session
joins its VM, actual user, and workspace.

Enable the selected integration's system plugin when required (for example, add `codex` to
`plugins.system` in the operator configuration). Plugin availability and facet activation are
separate choices. Activation is explicit even with default configuration. Core passes through an
inactive integration's inputs. An inactive VM routes its artifacts to user by default. Activating
only the user facet can therefore handle VM declarations at that actual user's native location. An
inactive user or workspace facet passes its applicable inputs onward to session. An activated VM
facet can route its inputs to user, workspace, or directly to session. Each deferral has one
destination, avoiding duplicate delivery through the user/workspace diamond. Handled payloads stop
at the facet that handled them. Each actual user handles the reusable VM inputs independently;
configuring the administrator does not configure agent users.

## A minimal bundle and consumer

Save these declarations in your operator resources directory. The bundle declares content; the VM
selects it; the agent activates the handler for that agent's Linux user. Shell is built in, so this
example does not require another system plugin or a model call.

```yaml
apiVersion: agentworks/v1
kind: artifact-bundle
metadata:
  name: setup-notes
spec:
  hints:
    tools:
      text: Project tools are available through mise.
---
apiVersion: agentworks/v1
kind: vm-template
metadata:
  name: artifact-vm
spec:
  artifacts:
    bundles: [setup-notes]
---
apiVersion: agentworks/v1
kind: agent-template
metadata:
  name: artifact-user
spec:
  harness_integrations:
    shell: {}
---
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: artifact-session
spec:
  harness_integration:
    name: shell
```

Select these templates through your normal VM, agent and session declarations or creation workflow.
The VM has no activated shell facet, so core makes its captured hint available to the user facet.
Agent setup handles it as a file for that actual user. A shell session under that agent does not
receive the handled hint again. If the user facet were inactive, the hint would instead reach
session and be published in its private artifact directory. Inactive workspace facets likewise pass
their applicable inputs to session. Recorded session deferrals resolve against the original owner's
capture, even if an upstream facet later changes its routing. If that content is no longer readable,
Agentworks reports the unresolved route and its owning repair action without inventing content.
Confirmed empty deferrals remain a known result even when the ancestor has incomplete native setup;
they do not prevent descendants from applying their own content.

Use `agw artifact show --agent <agent-name> --integration shell` to inspect the captured input and
recorded handling. The templates themselves do not create instances. See `agw vm create --help`,
`agw agent create --help` and `agw session create --help` for your normal instance workflow.

To inspect artifact delivery, use
`agw artifact show --session <session-name> --integration <integration-name>`. Read the VM, actual
user, workspace, then session rows in that order. An incomplete ancestor has unfinished application
or cleanup. Native-only incomplete setup also carries its recorded entry-specific reasons; resolve
the retained conflict before rerunning the owning setup. A stale ancestor reports changed
integration config keys, declared environment variable names by scope, or prepared artifact names,
without their values. Each owner's status is independent; the session row describes its own recorded
application. Missing session payloads or unknown session routing remain explicitly incomplete, with
recorded unhandled input identities where available. Ancestor problems are reported at managed start
without blocking launch. An inactive session integration has no setup obligation for that facet.
Inspection reports recorded application, not a fresh check of native files or model context.

## Two concurrent sessions

Copy [the concurrent-session declarations](examples/concurrent-sessions.yaml) into your operator
resources directory. They define two inline hint bundles and an explicit Shell session template,
without fetching sources or creating instances. Choose an existing workspace and an existing agent
with access to it, replacing `my-workspace` and `my-agent` below. If session creation is outside the
requested work, inspect existing sessions instead with `agw session list --workspace my-workspace`
and `agw artifact show --session <existing-session> --integration shell`.

These commands create and start two concurrent Shell workloads as the same agent in the selected
workspace. They create no VM, agent or workspace:

```bash
agw session create artifact-a --workspace my-workspace --agent my-agent \
  --template concurrent-shell --spec '{"artifacts":{"bundles":["session-notes-a"]}}'
agw session create artifact-b --workspace my-workspace --agent my-agent \
  --template concurrent-shell --spec '{"artifacts":{"bundles":["session-notes-b"]}}'
agw artifact show --session artifact-a --integration shell --output json
agw artifact show --session artifact-b --integration shell --output json
```

The session owner's `integrations[].placements[].path` values show the published `index.json` and
`scopes/session/<session-name>/hints/task-note.md`. Both hints have the same artifact name, but each
session has a different stable `session_uuid` and its own per-launch `run_id`:

```text
~/.agentworks-artifacts/session/<uuid-a>/<run-a>/scopes/session/artifact-a/hints/task-note.md
~/.agentworks-artifacts/session/<uuid-b>/<run-b>/scopes/session/artifact-b/hints/task-note.md
```

Use `agw session attach artifact-a` (or `artifact-b`) to enter that shell. There,
`printf '%s\n' "$AGENTWORKS_ARTIFACTS_DIR"` prints its current run directory, and
`cat "$AGENTWORKS_ARTIFACTS_DIR/index.json"` shows its own artifact index. Shell publishes context
files for tools to read; it does not run the hint text as instructions.

Separate run paths prevent publication collisions. Both processes share the same Linux user and can
read that user's session files; these paths provide no security isolation between them. Sessions
running as different Linux users use their respective private homes. Workspace artifacts remain
shared with workspace users, so keep session-only content in the session bundle selection.

A managed restart gives that session a new `run_id`, retaining its `session_uuid`. Old files stay
available if preparation fails and retire after the old runtime is torn down. To end this example,
leave any attached shell and delete both sessions:

```bash
agw session delete artifact-a
agw session delete artifact-b
```

Deletion stops each workload and retires its unchanged owned session files. Changed or unowned files
remain protected, and empty parent directories can remain. The bundles, template, selected agent,
and shared workspace remain available.

## Composition and names

Bundles contain four maps: `hints`, `rules`, `skills`, and `agents`. Each map key is the artifact's
canonical name. Skill and persona frontmatter must agree with that key; skill names also match the
selected directory. Names use at most 64 lowercase letters, digits and single hyphens.

A bundle can `inherits` other bundles through ordinary resource inheritance. Each type map merges by
key, replacing an overridden definition completely. A child `source` replaces a parent's inline
`text`; an overridden skill replaces the entire package. Discarded parent sources are not acquired.

Within one owner, later entries in `artifacts.bundles` replace earlier definitions with the same
type and key. Inspection retains compact evidence of those replacements. Changed content emits a
warning; identical content stays quiet and still records the winning source. Traversal uses hints,
rules, skills, then agents, preserving key insertion order within each map. Replacement keeps that
key's position. Different artifact types do not compete.

Each actual owner keeps its own group. A deferred VM group remains a VM group through user and
session routing; it never becomes part of the receiver's local group. The same type and key from
different scopes remain separate contributions. Integrations must preserve them through supported
native placement or aggregation, or refuse ambiguous delivery explicitly.

## Artifact formats

Hints are plain UTF-8 text. Skills use the standard
[Agent Skills format](https://agentskills.io/specification): `SKILL.md` and its complete supporting
file tree. Rules and agents use Markdown with the metadata contracts below. Text line endings are
normalized to LF.

### Rules

Rules are standing guidance intended to apply throughout work in their scope. Integrations use a
native rule file or a generated required-read skill; automatic body insertion is not universal.
Supply plain Markdown or a YAML frontmatter block at the start of the inline `text` or selected
file:

```markdown
---
description: Requirements for repository changes
---

Run the relevant checks before submitting changes. Keep documentation consistent with behavior.
```

`description` is the only supported rule field. It is optional; when supplied, it must be a nonempty
string of at most 1,024 characters. The containing `rules` map key names the rule. The instruction
body must be nonempty. Frontmatter must be a mapping, use unique string keys, and end with a second
`---` line. A leading `---` reserves that block for metadata; use `***` for a horizontal rule at the
start of plain Markdown.

`agw artifact show` exposes captured descriptions in text and machine output without fetching
sources or displaying instruction bodies. Descriptions describe artifacts for inspection; they do
not control when instructions load. Integrations render the body without its source frontmatter.

Unexpected fields are errors, including `name`, `globs`, `targets`, `root`, `localRoot` and native
option blocks. Conditional rules and per-artifact harness selection are unsupported. The consuming
scope and its activated integration facets determine placement and delivery.

### Agents

An agent persona requires `name`, `description` and a nonempty Markdown instruction body:

```markdown
---
name: reviewer
description: Review changes for correctness and unnecessary complexity
native_options:
  claude-code:
    tools: [Read, Grep, Glob]
  codex:
    model_reasoning_effort: high
---

Review the changes and report actionable findings.
```

The name must match its `agents` map key. The description must be a nonempty string of at most 1,024
characters. `native_options` is optional and maps integration names to option objects. Each
consuming integration validates its own supported options and renders the shared persona body into
its native format. An option for one integration does not configure another.

All other top-level fields are errors. For example, a top-level `model`, `tools`, `targets` or
Rulesync `claudecode` block is refused; supported native settings belong under
`native_options.<integration-name>`. Duplicate keys, non-string keys, YAML aliases and anchors are
also refused in rule and agent metadata. Hooks and MCP configuration remain unsupported.

These authoring formats borrow Rulesync's common-body and native-options approach without claiming
full Rulesync format compatibility. They do not change bundle selection, scope ownership or native
collision handling.

## Sources and refresh

Hints provide small setup facts. Rules provide standing guidance. Each accepts exactly one inline
`text` or a file `source`. Skills select an explicit directory containing `SKILL.md`, with the
complete supporting file tree. Agents select Markdown with `name` and `description` frontmatter and
an instruction body. The [artifact formats](#artifact-formats) above define accepted metadata for
each type.

Sources use workstation files or Git references, such as `file::~/agent-content/rules.md` and
`git::https://github.com/example/agent-content.git//skills/review?ref=v1.0.0`. Workstation `~`
refers to the operator's home. Git refs resolve once per capture; descendants consume that capture
without fetching again. VM and admin capture share the same reference resolution during their setup
operation. Pin a commit for reproducible updates. Do not put credentials in source URLs.

Text uses Unix line endings. Skill supporting files retain executable intent and binary bytes;
`preserve_bytes` names relative paths or globs for text fixtures that require exact bytes.
`SKILL.md` cannot be exempted from normalization. Review imported content before authorizing its
use: acquisition copies content and does not run package installers or skill scripts. Local and Git
sources reject unresolved Git LFS pointers. Relative member paths are limited to 4,096 characters
and 32 components; a capture must satisfy the persisted format before setup can accept it.

Existing captures keep the authoring interpretation recorded when they were acquired. Updating
Agentworks alone does not reinterpret YAML-looking rule text or remove previously retained persona
metadata. An explicit owning refresh captures sources under the current strict format; fix any
unsupported metadata before refreshing. Source changes alone do not update an existing capture.

VM/admin and agent changes take effect through the owning reinitialization operation. Workspaces
have no reinit operation; recreate the workspace when its artifact setup must change. Session
creation and managed launch prepare session-owned inputs. A stale ancestor requires its own setup
operation; starting a descendant reports it and continues with existing placement and known captured
deferrals. It does not repair or recapture the ancestor implicitly. Unknown handling is reported
without claiming delivery.

Applying files at an owning facet changes its native locations during that setup. A running workload
may pick up those files if its harness supports reloading them; Agentworks does not promise live
reload. Deferral instead records inputs for a later facet and does not update existing descendants.
User-routed changes take effect when the actual user's setup runs. Session-routed changes take
effect on a subsequent managed start or restart using the available captures and known routes.
Missing, stale or interrupted ancestor setup warns with its owner and supported repair operation.
Running sessions retain their existing delivery until then.

Workspace-routed changes have a stricter limit: workspace setup runs only during creation. There is
no workspace reinit, and workspace repair does not refresh artifacts. A stale existing workspace
cannot adopt those changes in place. Choosing to recreate it is a separate lifecycle decision, not
an automatic response to an artifact update. Setup warns about the delayed application of deferred
inputs without inspecting or modifying downstream owners. This timing also matters when inputs are
removed: updating an ancestor alone does not retire effects previously applied by a descendant.

Recreation is destructive. Preserve tracked edits, untracked files, commits not yet pushed and
stashes outside the workspace first. Correct and push the repository content the next clone needs,
or correct the configured workstation source/template. Editing only the old guest checkout does not
update the next workspace's inputs. An unknown applicable input set keeps a receiving facet
incomplete even if its local files apply. An unrelated ancestor native failure with a confirmed
routing table covering the current inputs does not make a completed receiver incomplete.

Applied config records identify files and registrations owned by the integration. Refresh removes
obsolete owned effects where supported. Retired skill files also prune empty parents up to their
recorded package root. The renderer supplies that exact boundary for every skill member; cleanup
never infers it from directory names. `SKILL.md` publishes before supporting files, so an
interrupted publication remains discoverable for retry. Supporting files retire before `SKILL.md`,
which stays owned and present while any owned supporting member remains. Nonempty directories and
modified or unowned files remain. Failed inner-directory cleanup retains the file record and package
boundary for retry, even when the file has already been removed. If only removing the final package
root is denied by permissions, cleanup verifies that directory's identity and emptiness, warns and
completes file retirement. Records that omit the optional package root retain entrypoint ordering
using the owned skill entrypoint path, but retire files without directory pruning. Existing unowned
content and files changed since publication require resolution rather than silent replacement,
except for the explicitly generated instruction sections described below. Removing an activation
with retained effects still needs the owning cleanup operation before passthrough can be considered
current. Deleting a VM retains the ordinary VM deletion behavior: its filesystem disappears with it.

## Native delivery

| Integration | VM facet                                           | User and workspace facets                                            | Session delivery                                                                  |
| ----------- | -------------------------------------------------- | -------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Shell       | All types under `/opt/agentworks/artifacts/`       | All types as files                                                   | All types as files, without opt-in                                                |
| Claude Code | Managed guidance skills, skills and agents         | Native rules, skills and agents                                      | Opt-in private plugin with guidance skills, skills and personas                   |
| Codex       | Machine guidance skills and skills; personas defer | Guidance skills, standard skills and native personas                 | Opt-in shared-home guidance/skills or private persona configuration               |
| Grok Build  | Defer to user                                      | User native rules; workspace guidance skills; native skills/personas | Opt-in shared-home entry with private indexed content; session personas unhandled |

Each rule has its own native file or generated skill. Hints aggregate once per original owning
scope. Generated helper names include an owner qualifier, preserving equal keys from distinct
scopes. The source declarations and their digests retain their original artifact types; projecting a
rule into a skill does not merge its key into the source `skills` map. Generated helpers use the
same native collision checks as authored skills. Standard skills retain complete package members.
Native invocation names may acquire a session qualifier or plugin namespace; inspection reports
recorded native identities and destinations. Literal invocation names inside imported skill bodies
are not rewritten.

Generated guidance skills contain a fixed `SKILL.md` entrypoint that directs the reader to sibling
`guidance.md`. That supporting file holds the literal rule body or owner hints aggregate. The
entrypoint includes its content revision, so changing the guidance also changes the entrypoint. Rule
and hint text therefore stays out of native skill-body command and variable preprocessing. Authored
skills retain their native semantics; using a skill can run native preprocessing permitted by the
harness. Reading a supporting file still requires tool access and agent cooperation.

Codex guidance uses generated skills under its ordinary skill roots; it does not edit AGENTS.md or
AGENTS.override.md. Claude user/project guidance uses separate native rule files. Grok workspace
guidance uses skills because Grok skips ignored rule files; user guidance uses separate native
rules. Native descriptions request reading standing guidance before working. A file/catalog entry
proves availability, not that a model loaded or obeyed it, and previously loaded text may remain in
history.

Codex project persona discovery also follows its native project-trust policy. Publishing a persona
under `<workspace>/.codex/agents/` does not grant that trust. In an untrusted project, Codex can
disable project configuration and omit those roles from its agent catalog; trust the project through
Codex before relying on its project personas. User-level persona discovery is a separate native
scope.

Previously recorded generated instruction sections are retired through their
`<!-- BEGIN AGENTWORKS GENERATED -->` and `<!-- END AGENTWORKS GENERATED -->` delimiters. Retirement
removes everything between a correctly paired set of delimiters, including later operator edits
inside that managed section, plus the markers. It preserves surrounding bytes, ownership,
permissions and extended attributes. Ambiguous or malformed markers are retained and diagnosed with
cleanup evidence. An otherwise empty shared document is retained without whole-file ownership. There
is no new shared instruction-section application.

Claude managed skills and personas use `/etc/claude-code/.claude/skills/` and
`/etc/claude-code/.claude/agents/`; generated VM guidance uses skills. Codex machine skills and
guidance skills use `/etc/codex/skills/`. VM files are root-owned and readable by VM users; setup
never consults descendant activation. Grok defers VM inputs to native user placement.

Confirmed workspace artifact files receive exact repository-local exclusions in Git-resolved
`info/exclude`. Git metadata files and linked worktrees are supported; linked worktrees can share
that metadata. An exclusion also ignores an unrelated untracked file at the same repository-relative
path in another worktree sharing it. Ordinary applied state records ownership of each marked pattern
pair. Updates/removal retire only unchanged owned pairs; unrelated exclusions and operator
modifications remain. Workspace deletion retires known pairs before removing its directory and
ownership records. If that cleanup cannot finish safely, deletion retains the workspace and evidence
for retry. Resolve the reported conflict, then retry the deletion. Unreadable native setup evidence
cannot identify the pairs; that limitation warns while ordinary workspace deletion continues.
Whole-VM deletion does not require guest artifact cleanup. Tracked artifact files stay tracked and
visible, with a warning. Exclusions cannot conceal tracked changes. Agentworks does not edit shared
`.gitignore`, hide whole native directories or change index flags.

Elevated native file operations share a fixed allowlist: `/etc/claude-code`, `/etc/codex` and
`/opt/agentworks/artifacts`. Reads, writes and cleanup outside those trees are refused, including
cleanup paths from previously applied state. This guards file-helper use; it does not sandbox
integration code or remove the guest account's existing privileges.

Application prints one line per applied artifact type, counting a skill package once. Up to three
names are shown in full; larger groups show the first two and `...`. Deferral output similarly names
the affected artifacts, destination and reason. A later setup or restart reconsiders those inputs;
delivery still depends on the receiving integration and any required workaround.

The native adapters check skill and persona identities across scopes. Before new delivery, a bounded
inventory checks existing entries in known machine, user and direct workspace discovery roots. It
reads native metadata names, including Codex configuration registrations, rather than assuming
filenames are names. Conflicts identify the native name and competing paths. During setup,
conflicting artifacts are deferred individually to the next supported facet while independent
artifacts continue. During VM, user and workspace setup, missing, unreadable or stale ancestor
captures cannot authorize retirement. Unresolved inherited origins protect affected prior placements
and related packages against replacement and removal until their source evidence is current.
Independently attributable local updates, removals and additions still converge. Attribution uses
the prior and current local captures; shared files and old placements whose origins can no longer be
classified remain held. Repairing the upstream owner and retrying the receiving owner's supported
setup removes that hold. Ordinary safe retirement removes prior owned placements no longer in that
plan; modified or unowned files remain protected. If session delivery still has a native-name
conflict, the affected inputs are reported as unhandled and excluded from both files and launch
arguments; independent delivery and launch continue. A non-conflicting native namespace can allow a
later facet to handle a previously deferred input. Problems solely in existing ancestor placement
also warn without blocking launch.

Local setup completion records the known inputs actually supplied to its invocation. Unknown
incoming-route health is calculated lazily from the ancestor records, rather than saved as a
permanent local failure. A workspace created while its upstream routing is unknown can become
current without another setup invocation when the repaired upstream handles its own inputs. If the
repaired upstream instead routes new content to that workspace, its artifact placement remains
noncurrent until that content is actually applied. Explicit required/recommended native setup
prerequisites retain their separate configuration, destination and local completion checks.

Inventory covers selected skill/persona types, with at most 512 directory entries, YAML headers up
to 32 KiB each and 1 MiB combined, and an Agentworks rendered-TOML budget for Codex personas of 32
MiB each and 64 MiB combined. The TOML budget is an Agentworks limit, not a native Codex product
limit. For Codex, Agentworks recursively checks package contents and accepts skill entrypoints only
at `<skill-root>/<package>/SKILL.md`. Its 512-entry budget includes supporting files and
directories, including proposed members and their implied directories. The Claude and Grok adapters
do not scan beneath a present package entrypoint. These native support limits do not restrict the
generic capture format. Before publication, preflight checks existing and proposed entries together,
counting a replaced path once, so an accepted plan fits the next inventory. Generated Codex persona
files obey the same per-file bound. YAML metadata rejects aliases, anchors and nesting deeper than
32 levels before constructing values. Directories without a root `SKILL.md` are walked within the
same entry budget: ordinary files are ignored, while nested `SKILL.md` candidates, symlinks and
special files are refused. This permits unowned notes to survive skill retirement without keeping
the removed skill active. Symlinked or nested candidate layouts are explicitly unsupported. These
are conservative Agentworks support limits, not claims that those layouts are invalid native
configurations. The inventory does not claim to cover additional ancestor repository roots,
third-party plugin locations, Codex system-configuration policy, or changes after preflight. Unknown
native discovery extensions require separate verification; a successful preflight is not an
exhaustive native inventory.

There is no separate session filesystem. Private session files use the actual user's
`~/.agentworks-artifacts/session/<session_uuid>/<run_id>/` directory. The opt-in Codex/Grok
home-skill workaround also uses a bounded session-owned package in that user's native skill root.
Other sessions of the same Linux user may discover their names, descriptions and paths.
Applicability is semantic and best effort; it is not a native catalog or filesystem isolation
boundary. No session artifact is published in a shared workspace.

`AGENTWORKS_SESSION_UUID` and `AGENTWORKS_RUN_ID` identify the managed logical session and current
run inside its workload. They carry the same values as Agentworks' private recovery fields and are
separate from the reusable display name and native harness conversation ID. Codex home-skill
descriptions and bodies state the exact session/run condition. Grok uses one stable session-UUID
entry that reads the current `AGENTWORKS_ARTIFACTS_DIR/index.json`; this keeps its cached native
catalog address usable across managed runs. Only the matching workload is instructed to use its
entries. Already-loaded conversation text remains native harness behavior.

Session delivery is disabled by default for native harnesses. Unhandled inputs warn with their
source and reason; unavailable inputs retain identity/source repair diagnostics without invented
content. They do not prevent launch. Malformed inputs, unsafe publication and new-delivery transport
failures remain errors. Prospective content uses disjoint run-specific paths; Grok's immutable entry
can be adopted at its existing path without changing the old runtime's index environment. Existing
runtime files survive failed preparation and retire safely after teardown. Differing unowned content
is preserved; modified obsolete files retain cleanup evidence. Staging and cleanup validate every
exact session-owned skill package before using its native skills parent as a filesystem publication
boundary. HOME itself is never a broad publication boundary for these session files.

Selecting a native persona and delivering its definition are separate operations. If a managed
persona definition remains unhandled, an explicit integration `agent` selection still applies. The
native harness may select its existing same-named persona; the warning does not claim that the
Agentworks definition was delivered.

For a delivered Claude plugin persona, an explicit matching `agent` selection uses its qualified
plugin identity. An unset selection does not implicitly select a managed persona. Native prompt
configuration, such as Claude `append_system_prompt`, Codex `developer_instructions` or Grok
`rules`, remains separate from artifacts and forwards at launch. Native resume may retain earlier
context; the artifact file carriers do not refresh those independent settings.

Adopting matching artifact files makes them managed and eligible for safe cleanup when removed from
the declaration. Confirmed file and generated-section removals and updates report bounded summaries.
Cleanup retires owned files and explicit skill package roots. Empty session/run directories can
remain; they grant no ownership over their parents and do not retain artifact content. Shell's guest
index retains applicability and revision metadata without workstation acquisition paths; workstation
inspection still shows source provenance.

A managed restart prepares the next run before replacing the old runtime. Old private-run files
remain available through failed preparation and retire only after the old runtime is torn down. If a
later run omits an unavailable input, its incomplete/unhandled evidence reports that omission.

This delivery includes declared bundles and harness handling. Core hint emission, features, hooks,
and MCP artifacts are later work.

## Session workarounds

Claude, Codex and Grok start with `enabled_workarounds: []` in their session facets. Enable only the
specific delivery methods you need. A workaround is permission to use that method, not a promise
that the native harness supports every artifact or option. An incompatible native CLI can still
reject the explicitly selected launch arguments. Unknown names are configuration errors. The list
replaces the inherited list; an explicit `[]` disables inherited workarounds. Setup-facet
activations do not enable session workarounds.

| Integration | Workaround                  | Session inputs and behavior                                                                |
| ----------- | --------------------------- | ------------------------------------------------------------------------------------------ |
| Claude Code | `session-skill-plugin`      | Rules, aggregate hints and complete skills in a private native plugin.                     |
| Claude Code | `session-agent-definitions` | Native persona files in the same private plugin.                                           |
| Codex       | `session-home-skills`       | Session-qualified rules, aggregate hints and skills in `~/.agents/skills`.                 |
| Codex       | `session-agent-config`      | Private native persona configuration through launch overrides.                             |
| Grok Build  | `session-home-skills`       | One session-specific native entry and a private index of rules, hints and complete skills. |

The home-skill methods warn that the native catalog is shared and matching is best effort. They
never toggle shared native enablement settings or rebuild unrelated operator selector arrays. Codex
registers qualified individual skill entries. Grok instead registers one immutable session-specific
entry: its body reads the current run's index and accesses canonical skills through listed files.
Those skills have no individual native slash commands, activation or tool controls. Skills
explicitly using Grok activation fields `paths`, `disable-model-invocation` or `user-invocable`
remain unhandled because indexed access cannot preserve those controls. Grok's `allowed-tools`
metadata does not itself grant or restrict tools. Session personas remain unhandled; explicit native
`agent` selection is separate.

Grok resume can retain a native catalog from before the session entry was first installed. If that
entry is absent, start a fresh conversation with `--force-new` or explicitly read the current
`$AGENTWORKS_ARTIFACTS_DIR/index.json` in the workload. Once advertised, the stable entry points to
current runtime environment rather than embedding an earlier run's index path. This does not force
rereading or erase old already-loaded guidance; model cooperation remains necessary.

Shell needs no workaround and exposes its session index through `AGENTWORKS_ARTIFACTS_DIR`.
Workarounds never implicitly activate an ancestor facet. Removing an opt-in retires its recorded
session-owned files on the next managed launch; already-applied ancestor files remain owned there.
There is no artifact-specific native version/snapshot probing or prompt-body launch injection.

For example, enable Claude session guidance and skills while leaving session personas off:

```yaml
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: session-guidance
spec:
  harness_integration:
    name: claude-code
    enabled_workarounds: [session-skill-plugin]
```

Select bundles through the ordinary `artifacts.bundles` block. This declares configuration only;
application happens during a subsequent managed launch. Native skill invocation/reading is distinct
from discovering the package, and updating files does not erase previously loaded conversation text.

## Upgrade existing artifact setup

Update selected session declarations when upgrading from the older prompt carriers:

| Previous workaround                    | Current declaration                                     |
| -------------------------------------- | ------------------------------------------------------- |
| Claude `session-prompt`                | `session-skill-plugin` for guidance delivered as skills |
| Codex `session-developer-instructions` | `session-home-skills`                                   |
| Grok `session-rules`                   | `session-home-skills`                                   |
| Grok `session-agent-definitions`       | Remove it; session personas remain unhandled            |

The removed spellings are configuration errors. Claude `session-agent-definitions` and Codex
`session-agent-config` retain native persona delivery. Existing ancestor output changes through its
own supported lifecycle: VM/user reinit can retire recorded old instruction sections and publish new
guidance files. Existing workspaces have no reinit; preserve work and use the supported workspace
replacement lifecycle when changing their owned setup. Session restart does not repair ancestors. A
session's own supported restart reconciles its recorded files and current workarounds.

## Inspect before changing state

```sh
agw artifact show --vm dev
agw artifact show --vm dev --admin
agw artifact show --agent developer
agw artifact show --workspace service
agw artifact show --session review --integration codex
```

Inspection includes applicable ancestor content, even artifacts already handled upstream. It shows
current declarations, captured revisions, activation, recorded handling and deferral, and native
placement, current winning provenance and compact replacement evidence. Each type/key row includes
all current contributing bundle IDs in selection order, with the last selected. This declaration
selection is separate from captured provenance and replacement digests, including when capture is
missing or stale. Missing captures remain unknown. Stale, interrupted, or malformed evidence cannot
become an empty successful result. Selectors must describe one actual lineage.

The command reads existing state and declarations. It does not fetch sources, apply integrations,
resolve secrets, or attest to the current filesystem or model context. Artifact bodies are omitted.
To change setup, first establish authorization for the owning operation and inspect its impact.

## Implementation boundaries

`capture.py` acquires workstation files/trees or immutable Git objects through `package_sources.py`.
It normalizes designated UTF-8 text to LF, preserves binary bytes and executable intent, and emits
the frozen groups and values in `model.py`. Skills are complete standard Agent Skills packages.
Agents are agent personas. Sources are edge concerns: routing and integration APIs consume the same
normalized inputs regardless of their source.

`codec.py` validates the bounded lossless wire representation. `state.py` records one common core
capture per owner component in the existing instance-state store. `routing.py` reads that capture
and the selected integration's reusable results along one actual VM/user/workspace/session graph.
Handling an input for one user does not consume the VM result for another user.

An unavailable explicit route retains its input identity and source repair reason without a payload.
Integrations receive only readable inputs. After validating an active receiver's result, core
forwards the unavailable identities in its recorded input and deferral metadata, including on first
application. Inactive receivers preserve the same metadata through fixed passthrough. Missing
content stays incomplete through the session; it is never counted as delivered.

`application.py` defines integration results, file/section ownership and recorded skips.
`publication.py` validates plugin output and uses the shared guarded `NativeFiles` transport
utility. Native formats and policy live in integration adapters. Existing files whose bytes match
the declaration can be adopted, with recorded ownership and a report. Differing unowned or manually
changed files are preserved with recorded skips and evidence for retry; independent artifacts still
apply. A conflicting member skips its whole skill package, including when a modified obsolete member
must be retained while the package is still requested. Generated instruction sections instead follow
their explicit replaceable-section contract.

`session.py` prepares the launch context, stages a new private run before old-runtime teardown, then
retires obsolete owned files after teardown. No artifact bookkeeping changes VM deletion.
`agw artifact show` projects declarations, captures and recorded delivery without claiming to
observe the target filesystem or prove that a model obeyed the supplied content.
