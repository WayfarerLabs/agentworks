# Runtime context templates: requirements

Status: draft for operator review. This is a new effort designated by the operator on 2026-11-06.
Requirements below distinguish operator direction from proposed behavior.

## Problem

An env value in a reusable template cannot currently name a path beneath the eventual target user's
home directory. Hardcoding the path in an instance spec works for one identity but defeats template
reuse. Existing session placeholders address only a small part of this problem.

## Operator direction

- Introduce placeholders for the target user's home directory and other appropriate context values.
- Use `user_home_dir`, consistent with `workspace_dir`.
- Design the mechanism for reuse beyond env values; the existing shell integration's commands are
  relevant prior art within Agentworks, but their syntax is not a constraint.
- Investigate references to shell variables, including the suggested `{{$$SHELL_VAR}}` spelling.
- Research external prior art.
- Establish a new SDD and produce permanent documentation outside its directory.

Delivery ruling from the operator:

> Please note that we're going to end up implementing the whole feature in a single PR (including
> the full SDD).

## Proposed functional requirements

### FR1. Reusable target context

The same env definition works for different agents and for admin sessions without embedding a Linux
username. `{{user_home_dir}}/.config/tool` resolves beneath the consuming user's managed home,
regardless of which scope declared the value. The consuming user is the selected workload or setup
identity, not the operator running `agw` or an administrative transport hop.

### FR2. Initial vocabulary

Support `user_home_dir`, `user_name`, `vm_name`, `agent_name`, `workspace_name`, `workspace_dir`,
and `session_name`. Keep the existing `{{session_name}}` and `{{workspace_name}}` spellings and
meanings. Context values unavailable to an invocation produce an actionable error when referenced.

### FR3. Env coverage and precedence

Support plaintext env values from VM, workspace, admin, agent, and session templates and their
instance overlays. Preserve inheritance and scope precedence. Expand only winning entries, against
the consuming invocation's context. Do not expand variable names, secret reference names, or secret
contents. An overridden entry cannot fail because its placeholder is unavailable at runtime.

### FR4. Explicit interpolation contract

Provide one documented placeholder grammar, literal escaping, and one-pass substitution. Unknown
names and malformed placeholder syntax fail with field context. Replacement text is data and is
never scanned again for placeholders. Do not perform shell evaluation, command substitution,
recursive env dependencies, expressions, filters, or implicit workstation-environment lookup.

### FR5. Existing command consumers

Use the same vocabulary and parsing semantics for the existing command interpolation sites,
including shell `command` and `resume_command`. Preserve literal harness prompts and other workload
data. Rendering an env value and rendering a shell program have different quoting requirements;
sharing context lookup must not turn ordinary values into shell source.

### FR6. Inspection and lifecycle

`env show` renders the same winning plaintext values as execution for equivalent context, without
connecting to a VM or waking it. Secret values retain existing redaction and explicit resolution
behavior. A restart recomputes values for its current context. Invalid templates fail before
creating resources or tearing down an existing running session where context is already known.
Checks requiring a newly created identity occur as soon as that identity is determined, before
launch or destructive replacement. Existing processes do not acquire changed values automatically.

### FR7. Permanent reference

Maintain `docs/arch/runtime-context.md` as the permanent architecture and extension reference. The
implementation change updates it with the complete vocabulary, availability, escaping, expansion
order, command handling, and examples. CLI and schema-owned descriptions link to or summarize this
reference without requiring access to the SDD.

## Proposed scope: environment references

The operator clarified the intended source and raised a related question:

> I was thinking the target user's environment, but the more I think about this the less I like it.
> Can you reference agw-managed vars? It just seems messy.

Recommendation for draft acceptance: omit ambient environment lookup and references between managed
env entries from the initial increment. Context placeholders expose the underlying identity facts
directly; for example, `workspace_dir` supplies the value represented by `AGENTWORKS_WORKSPACE_DIR`.
Arbitrary managed entries are not added to the context catalogue.

Reading another managed env entry would need dependency ordering, cycle handling, and secret
propagation rules. Reading the target's environment would need shell startup, timing, sensitivity,
and inspection rules. Neither behavior is necessary for `user_home_dir`. The omission remains a
design recommendation pending acceptance of this draft, not a claim that the operator approved a new
requirement. See [architecture](hla.md#environment-reference-decision).

## Acceptance examples

- Two agents consuming `CACHE_DIR: "{{user_home_dir}}/.cache/tool"` receive different paths beneath
  their respective managed homes; an admin shell receives the admin user's path.
- The same expression works when declared at VM or workspace scope and consumed as an agent.
- A session-only value referencing `session_name` works for session launch. A winning reference to
  it in a standalone agent shell errors rather than inventing a session.
- A secret containing `{{session_name}}` reaches the application literally.
- Default `env show --session NAME` previews expanded plaintext and redacted secrets offline.
- Shell commands continue accepting `$HOME` under ordinary shell quoting and execution rules; an env
  value containing `$HOME` alone remains literal unless an explicit env-lookup feature is separately
  accepted.
