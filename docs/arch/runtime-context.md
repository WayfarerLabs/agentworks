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
applies only to the reserved namespace. In command source, this suppresses Agentworks expansion; the
resulting text still follows shell quoting rules. Ordinary `$HOME`, `${HOME}`, `${PATH:-default}`,
`$$`, Go-template text, and unrelated braces are unchanged by the default renderer unless a dollar
sign immediately precedes a reserved reference: `$$${agw:user_name}` becomes `$${agw:user_name}`
because the last two dollar signs start the namespace escape.

Rendering takes one pass. Namespace-looking text in a replacement value is not interpreted again.
There are no expressions, filters, ambient lookups, or references between env entries.

Shell's `command` and `resume_command` temporarily also accept `{{session_name}}` and
`{{workspace_name}}`. They mean `${agw:session_name}` and `${agw:workspace_name}` respectively. Only
these two exact aliases are recognized; other brace text remains literal. Both syntaxes render in
one pass, so inserted values containing either syntax are not scanned again. A dollar before an old
token remains a literal dollar followed by its replacement.

These Shell aliases are deprecated through CLI 0.20.x and 0.21.x, with deliberate removal planned
for 0.22.0 in [issue #878](https://github.com/WayfarerLabs/agentworks/issues/878). Affected launches
warn once per CLI request; `--no-deprecations` suppresses that notice. `agw doctor` reports affected
effective templates and stored session overrides even when ambient warnings are suppressed. Env
values and native integration arguments use only the namespaced syntax. See the
[compatibility and deprecation pattern](deprecations.md).

## Scope, lifecycle, and inspection

Template inheritance and stored instance overlays produce each scope's effective declarations. Merge
the applicable scopes before rendering their winning plaintext entries:

```text
VM < workspace < agent or admin < session
```

Later scopes win on a matching key. Agent and admin are alternatives selected by the consuming Linux
user. Applicable managed identity variables win before rendering or secret collection. Discarded
entries are neither rendered nor resolved.

| Invocation                   | Context                                                             |
| ---------------------------- | ------------------------------------------------------------------- |
| VM shell/exec                | VM admin; optional selected workspace                               |
| Agent shell/exec             | Agent's Linux user and resource name; optional authorized workspace |
| Session create/start/restart | Selected admin or agent; workspace and session                      |
| Console extra shell          | Selected admin or agent and workspace; no session                   |
| VM/admin setup               | VM admin; no workspace, agent, or session                           |
| Agent setup                  | Agent's Linux user and resource name                                |
| Workspace setup              | VM admin and workspace                                              |

Existing processes keep their initial environment. A new invocation recomputes values. Invalid env
references are checked before workload secret prompts and mutations where context is known. Shell
preflight checks that its selected `command` or `resume_command` renders, including for a planned
agent or workspace, before workload/env secret resolution. The existing VM activation gate may need
its own credentials before remote preflight.

The readiness path checks declared `required_commands` on an available target; a new target's probe
defers until that target exists. These checks cover whether the template renders and whether
declared commands are available. Command behavior is determined by execution. Launch renders the
selected field again from its declaration before launch or destructive replacement. Native
transports retain their documented env-delivery limitations.

`agw env show --session NAME` previews winning plaintext values and their scopes from managed state
without connecting to the VM. `--vm`, `--workspace`, and `--agent` select other contexts. Secrets
remain redacted unless `--resolve` is explicit. This is a managed-config preview, not an observation
of a process's live environment or user shell startup effects.

SSH delivery uses the client's `SetEnv` option. Clients that expand SetEnv values can reinterpret
literal `${...}` text using the controller's environment, or refuse it when that variable is absent.
Ordinary `${agw:user_name}` references render to the consuming username before transport. Literal
namespace text can remain intentionally through plaintext escaping or opaque secret content. Preview
and setup rendering do not perform the SSH client expansion. This transport behavior is tracked in
[issue #873](https://github.com/WayfarerLabs/agentworks/issues/873).

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
integration quotes such values as argv data. Structured extra arguments render against the bound
invocation context before quoting, so a workspace path containing an apostrophe remains part of one
argument. Shell renders its selected command field as source. The manager consumes the integration's
returned command directly, so generated data and inserted namespace-looking text are not scanned
again. Shell variables and Go templates are processed only by their downstream consumers.

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
exists. Both use the same finite catalogue and context projection. Session preparation binds those
facts and the explicit launch intent before preflight, so shell checks only its selected field.

Render only fields whose contract calls for interpolation. Render winning plaintext env entries
without requiring secret values; keep provenance independent. Validate before side effects when
context is available. Treat env strings, argv elements, and shell source as different destinations:
quote argv elements once if they must be serialized into shell source. Each integration owns the
rendering of its declared template fields; returned command strings are ready for execution and
receive no further interpolation. No consumer may reinterpret secret contents as expressions. Shell
alone supplies its temporary exact-token alias map to the renderer; new consumers use the default
namespaced grammar.

See the [CLI session-template reference](../../cli/command-reference.md#session-templates) for the
shell integration configuration.

The shared implementation lives in `cli/agentworks/interpolation.py`: `context_values` supplies
known facts, `runtime_values` projects `ResourceContext`, and `render_template` performs the pure
substitution. `cli/agentworks/env/render.py` supplies `render_plaintext` for winning env entries;
`cli/agentworks/linux_users.py` owns the managed-home rule.
