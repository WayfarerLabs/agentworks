# Runtime context and string interpolation

Agentworks substitutes explicit resource context in plaintext env values and workload commands using
`${agw:NAME}`. It does not read arbitrary shell variables or build references between env entries.
Secret reference names and secret contents are literal data.

## Context values

| Name             | Meaning                            | Availability                         |
| ---------------- | ---------------------------------- | ------------------------------------ |
| `user_name`      | The consuming Linux username       | Every supported env composition      |
| `user_home_dir`  | That user's managed home directory | Every supported env composition      |
| `vm_name`        | The VM resource name               | Every supported env composition      |
| `agent_name`     | The agent resource name            | Agent execution and setup            |
| `workspace_name` | The workspace resource name        | Workspace-rooted execution and setup |
| `workspace_dir`  | The workspace root directory       | Same as `workspace_name`             |
| `session_name`   | The session resource name          | Session workload execution           |

The consuming identity determines the value, regardless of which template scope declares the
expression. Agent resource names and Linux usernames are different identifiers. Managed homes use
`/home/<Linux username>`, through the same rule used during initialization. Workstation `HOME` is
not consulted.

```yaml
env:
  TOOL_CONFIG_DIR: "${agw:user_home_dir}/.config/tool"
  PROJECT_DIR: "${agw:workspace_dir}"
```

An agent running as `agt-reviewer` receives `/home/agt-reviewer/.config/tool`; an admin shell
receives the admin user's home path. `PROJECT_DIR` requires workspace context. A winning reference
to an unavailable name fails rather than inventing a workspace, agent, or session.

## Syntax and literal text

A reference is `${agw:NAME}`, with an ASCII identifier as the name. Unknown names, missing context,
and malformed references within the reserved namespace fail with the owning field identified.
Whitespace inside a reference is not normalized.

Use `$${agw:user_name}` in a plaintext template to obtain literal `${agw:user_name}`. Escaping
applies only to the reserved namespace. Ordinary `$HOME`, `${HOME}`, `${PATH:-default}`, `$$`,
Go-template text, and unrelated braces are unchanged by Agentworks.

Rendering takes one pass. Namespace-looking text in a replacement value is not interpreted again.
There are no expressions, filters, ambient lookups, or references between env entries.

## Scope, lifecycle, and inspection

Template inheritance and stored instance overlays produce each scope's effective declarations. Merge
the applicable scopes before rendering their winning plaintext entries:

```text
VM < workspace < agent or admin < session
```

Later scopes win on a matching key. Agent and admin are alternatives selected by the consuming Linux
user. Discarded entries are not rendered. Managed identity variables retain their own precedence.

| Invocation                   | Context                                                             |
| ---------------------------- | ------------------------------------------------------------------- |
| VM shell/exec                | VM admin; optional selected workspace                               |
| Agent shell/exec             | Agent's Linux user and resource name; optional authorized workspace |
| Session create/start/restart | Selected admin or agent; workspace and session                      |
| Console extra shell          | Selected admin or agent and workspace; no session                   |
| VM/admin setup               | VM admin; no workspace, agent, or session                           |
| Agent setup                  | Agent's Linux user and resource name                                |
| Workspace setup              | VM admin and workspace                                              |

Existing processes keep their initial environment. A new invocation recomputes values. Invalid
references are checked before prompts and mutations where context is known, including before a
restart replaces the current runtime. Returned command source is validated after the integration
selects its launch command and before launch or destructive replacement. Native transports retain
their documented env-delivery limitations.

`agw env show --session NAME` previews winning plaintext values and their scopes from managed state
without connecting to the VM. `--vm`, `--workspace`, and `--agent` select other contexts. Secrets
remain redacted unless `--resolve` is explicit. This is a managed-config preview, not an observation
of a process's live environment or user shell startup effects.

## Secrets

Neither a secret reference name nor its resolved content is interpolated or unescaped. A secret
containing `${agw:user_name}` retains that text; a secret containing `$${agw:user_name}` retains
both dollar signs. Existing consumer safety checks still apply.

This differs intentionally from plaintext: a plaintext entry is a template, while a secret entry
provides literal data. Its declared type determines interpretation, independent of its contents.

## Commands and shell quoting

Env values are literal transport data. Commands are shell source, so substitution alone does not
quote an arbitrary value correctly for every position in a command.

```yaml
harness_integration:
  name: shell
  command: "tool --config '${agw:user_home_dir}/.config/tool'"
```

Managed Linux usernames make the home path suitable for that quoted example. For an arbitrary
workspace path, including spaces or apostrophes, use `"$AGENTWORKS_WORKSPACE_DIR"` in shell source.
The shell expands the variable as data within that argument. Raw substitution between single quotes
cannot represent an embedded apostrophe safely.

Native integration prompt/profile fields and other opaque workload input remain literal. The
integration shields reserved namespace text while quoting such values as argv data. Fields that
explicitly accept command arguments participate in context rendering according to their field
contract. Shell variables and Go templates are processed only by their downstream consumers.

## Identity env delivery

The shared resource context also supplies `AGENTWORKS_*` identity variables:

| Group      | Variables                                                                                                            | Delivery                |
| ---------- | -------------------------------------------------------------------------------------------------------------------- | ----------------------- |
| VM         | `AGENTWORKS_VM`, `AGENTWORKS_PLATFORM`, `AGENTWORKS_SITE`                                                            | System profile fragment |
| User       | `AGENTWORKS_AGENT` for agent users                                                                                   | User profile fragment   |
| Invocation | `AGENTWORKS_WORKSPACE`, `AGENTWORKS_WORKSPACE_DIR`, `AGENTWORKS_SESSION`, `AGENTWORKS_SESSION_KIND`, when applicable | Runtime env injection   |

`env show` includes invocation identity. VM and user identity also reach login shells through their
profile fragments. Placeholder values come from explicit context rather than reading these exports.

## Adding a consumer

Supply the managed identity and resource facts the invocation actually knows. Runtime callers use
the existing `ResourceContext`; preparation can supply planned facts before a full runtime context
exists. Both use the same finite catalogue and context projection.

Render only fields whose contract calls for interpolation. Render winning plaintext env entries
without requiring secret values; keep provenance independent. Validate before side effects when
context is available. Treat env strings, argv elements, and shell source as different destinations:
quote argv elements once if they must be serialized into shell source, and shield literal workload
input from later textual rendering. No consumer may reinterpret secret contents as expressions.

See the [CLI session-template reference](../../cli/command-reference.md#session-templates) for the
shell integration configuration.
