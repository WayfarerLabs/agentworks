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
  artifacts:
    tools:
      type: hint
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

## Sources and refresh

Hints provide small setup facts. Rules are instructions always loaded into context. Each accepts
exactly one inline `text` or a file `source`. Skills select an explicit directory containing
`SKILL.md`, with the complete supporting file tree. Agents select Markdown with `name` and
`description` frontmatter and an instruction body.

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

VM/admin and agent changes take effect through the owning reinitialization operation. Workspaces
have no reinit operation; recreate the workspace when its artifact setup must change. Session
creation and managed launch prepare session-owned inputs. A stale ancestor requires its own setup
operation; starting a descendant does not repair it implicitly.

Applied config records identify files and registrations owned by the integration. Refresh removes
obsolete owned effects where supported. Existing unowned content and files changed since publication
require resolution rather than silent replacement. Removing an activation with retained effects
still needs the owning cleanup operation before passthrough can be considered current. Deleting a VM
retains the ordinary VM deletion behavior: its filesystem disappears with it.

## Native delivery

| Integration | User and workspace facets                              | Private session delivery                                                                             |
| ----------- | ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| Shell       | Files for every artifact type                          | Files and an index through `AGENTWORKS_ARTIFACTS_DIR`                                                |
| Claude Code | Native rules, skills, and agent definitions            | Appended context, a session plugin for skills, and agent definitions                                 |
| Codex       | Native skills and agent definitions; hints/rules defer | Added developer instructions and private agent configuration; skills require earlier native handling |
| Grok Build  | Native rules, skills, and agent definitions            | Added rules and agent definitions; skills require earlier native handling                            |

Native identity can differ from bundle entry identity, including plugin skill namespaces. Inspection
shows recorded native names and locations. Rules remain unconditional; filename similarity alone
does not establish that a harness loads them as context.

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
placement. Missing captures remain unknown. Stale, interrupted, or malformed evidence cannot become
an empty successful result. Selectors must describe one actual lineage.

The command reads existing state and declarations. It does not fetch sources, apply integrations,
resolve secrets, or attest to the current filesystem or model context. Artifact bodies are omitted.
To change setup, first establish authorization for the owning operation and inspect its impact.

## Implementation boundaries

`capture.py` acquires workstation files/trees or immutable Git objects through `package_sources.py`.
It normalizes designated UTF-8 text to LF, preserves binary bytes and executable intent, and emits
the frozen values in `model.py`. Skills are complete standard Agent Skills packages. Agents are
agent personas. Sources are edge concerns: routing and integration APIs consume the same normalized
inputs regardless of their source.

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
