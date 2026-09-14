# Agent artifacts

An `artifact-bundle` declares reusable hints, rules, standard
[Agent Skills](https://agentskills.io/specification), and agents (agent personas). An owning
resource selects bundles with `artifacts.bundles`; its core setup captures their contents before
explicitly activated harness integrations handle or defer them.

Use `agw resource explain artifact-bundle` for the declaration schema and
`agw resource sample artifact-bundle` for an editable sample. Use `agw artifacts show --help` for
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
    - name: shell
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
session and be published beneath that user's private session artifact directory. Inactive workspace
facets likewise pass their applicable inputs to session.

Use `agw artifacts show --agent <agent-name> --integration shell` to inspect the captured input and
recorded handling. The templates themselves do not create instances. See `agw vm create --help`,
`agw agent create --help` and `agw session create --help` for your normal instance workflow.

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

Rules are always loaded into context. Supply plain Markdown or a YAML frontmatter block at the start
of the inline `text` or selected file:

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

`agw artifacts show` exposes captured descriptions in text and machine output without fetching
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

Hints provide small setup facts. Rules are instructions always loaded into context. Each accepts
exactly one inline `text` or a file `source`. Skills select an explicit directory containing
`SKILL.md`, with the complete supporting file tree. Agents select Markdown with `name` and
`description` frontmatter and an instruction body. The [artifact formats](#artifact-formats) above
define accepted metadata for each type.

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
operation; starting a descendant does not repair it implicitly.

Applying files at an owning facet changes its native locations during that setup. A running workload
may pick up those files if its harness supports reloading them; Agentworks does not promise live
reload. Deferral instead records inputs for a later facet and does not update existing descendants.
User-routed changes take effect when the actual user's setup runs. Session-routed changes take
effect on a subsequent managed start or restart, after any stale ancestors have been refreshed.
Running sessions retain their existing delivery until then.

Workspace-routed changes have a stricter limit: workspace setup runs only during creation. There is
no workspace reinit, and workspace repair does not refresh artifacts. A stale existing workspace
cannot adopt those changes in place. Choosing to recreate it is a separate lifecycle decision, not
an automatic response to an artifact update. Setup warns about the delayed application of deferred
inputs without inspecting or modifying downstream owners. This timing also matters when inputs are
removed: updating an ancestor alone does not retire effects previously applied by a descendant.

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
content and files changed since publication require resolution rather than silent replacement.
Removing an activation with retained effects still needs the owning cleanup operation before
passthrough can be considered current. Deleting a VM retains the ordinary VM deletion behavior: its
filesystem disappears with it.

## Native delivery

| Integration | User and workspace facets                              | Private session delivery                                                                             |
| ----------- | ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| Shell       | Files for every artifact type                          | Files and an index through `AGENTWORKS_ARTIFACTS_DIR`                                                |
| Claude Code | Native rules, skills, and agent definitions            | Appended context, a session plugin for skills, and agent definitions                                 |
| Codex       | Native skills and agent definitions; hints/rules defer | Added developer instructions and private agent configuration; skills require earlier native handling |
| Grok Build  | Native rules, skills, and agent definitions            | Added rules and agent definitions; skills require earlier native handling                            |

The map key supplies the native name, with a native namespace where required, such as Claude's
session skill plugin. Inspection shows recorded names and locations. Shell uses scope directories
and a grouped index, so equal names from different owners remain separately accessible. Claude and
Grok combine hints in `agentworks-hints.md` and same-key rules in a rule file at each destination.
Their user and project rule discovery is additive. Codex adds deferred hints and rules to session
developer instructions.

The native adapters refuse ambiguous skill or persona identities across scopes, including already
handled ancestors. Before managed launch, a bounded inventory also checks existing native entries in
the known user and direct workspace discovery roots. It reads native metadata names, including Codex
configuration registrations, rather than assuming filenames are names. Conflicts identify the native
name and competing paths. This check still runs when all applicable inputs were handled upstream.

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
third-party plugin locations, or changes after preflight. Unknown native discovery extensions
require separate verification; a successful preflight is not an exhaustive native inventory.

There is no separate session filesystem. Session publication uses the actual user's private
`~/.agentworks-artifacts/session/<session_uuid>/<run_id>/` directory. It avoids exposing session
content to other workspace users, but sessions sharing that Linux user share its access. A new
managed run uses a new directory; reusable session names do not reuse another session's identity.
Unsupported final-session delivery is a launch error, with the original artifact and reason.

This delivery includes declared bundles and harness handling. Core hint emission, features, hooks,
and MCP artifacts are later work.

## Inspect before changing state

```sh
agw artifacts show --vm dev
agw artifacts show --vm dev --admin
agw artifacts show --agent developer
agw artifacts show --workspace service
agw artifacts show --session review --integration codex
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

`application.py` defines integration results and whole-file ownership. `publication.py` validates
plugin output and uses the shared guarded `NativeFiles` transport utility. Native formats and policy
live in integration adapters. Existing unowned files are never adopted merely because their bytes
match. Changed managed files are retained and diagnosed, with evidence for retry.

`session.py` prepares the launch context, stages a new private run before old-runtime teardown, then
retires obsolete owned files after teardown. No artifact bookkeeping changes VM deletion.
`agw artifacts show` projects declarations, captures and recorded delivery without claiming to
observe the target filesystem or prove that a model obeyed the supplied content.
