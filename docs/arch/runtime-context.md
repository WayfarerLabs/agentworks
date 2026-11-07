# Runtime context and string interpolation

Agentworks has two related context mechanisms: identity env variables and session string
interpolation. This reference describes their implemented behavior and the boundaries a consumer
must respect.

## Context and identity

`ResourceContext` in `cli/agentworks/env/identity.py` describes the selected VM, platform, site,
Linux user, and optional agent, workspace, and session. The command or lifecycle operation supplies
these facts. An agent resource name and its Linux username are different identifiers.

Identity variables are produced from this context in three groups:

| Group      | Variables                                                                                                            | Delivery                  |
| ---------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------- |
| VM         | `AGENTWORKS_VM`, `AGENTWORKS_PLATFORM`, `AGENTWORKS_SITE`                                                            | System profile fragment   |
| User       | `AGENTWORKS_AGENT` for agent users                                                                                   | User profile fragment     |
| Invocation | `AGENTWORKS_WORKSPACE`, `AGENTWORKS_WORKSPACE_DIR`, `AGENTWORKS_SESSION`, `AGENTWORKS_SESSION_KIND`, when applicable | Runtime SSH env injection |

Console extra shell panes have workspace context but no session context. They are separate shells
alongside a workload. Setup operations likewise use the context of their executing identity:
workspace setup runs as admin; agent setup runs as the agent.

Managed agent and admin initialization currently use `/home/<linux_username>` as the user's home
path. A workstation's `HOME` does not identify a managed user's home.

## Env values

Env entries contain plaintext or a secret reference. Template inheritance and stored instance
overlays are resolved before the invocation merges the applicable scopes. Higher precedence wins:

```text
VM < workspace < agent or admin < session
```

Agent and admin are alternatives selected by the consuming Linux user. `compose_env` renders secret
references from the operation's eagerly resolved values and overlays dynamic identity variables. The
env map is literal data delivered to the transport, not shell program text. An env value containing
`$HOME/file` does not request shell expansion of that string.

`agw env show` independently assembles winning values and scope provenance from managed state. It
does not connect to the VM. Secret values remain redacted unless explicitly resolved. It includes
dynamic identity variables, while the VM and user identity groups come from on-VM profile files.

## Implemented string placeholders

The session manager supports `{{session_name}}` and `{{workspace_name}}`. It applies these to
plaintext session env entries before the cross-scope merge and to the launch command returned by a
harness integration during create/start/restart. Other env scopes and `env show` do not apply that
session substituter.

The current regex matches double braces around a word. It rejects matched unknown names; other brace
syntax can remain unchanged. There is no general documented literal-escape grammar or
ambient-environment lookup in this mechanism.

Launch commands are shell programs. Integrations use `quote_literal_argv` for workload fields whose
literal brace text must survive the manager's interpolation pass. Some integration extra args
intentionally allow the manager's placeholders. Ordinary shell variable references in a command
follow the executed shell's quoting rules.

## Consumer boundaries

Context lookup and shell quoting are separate responsibilities. An env value is a literal string; an
argv argument is one data item; a shell command is source code. A renderer that returns text does
not by itself provide shell escaping for arbitrary placement inside a shell program.

Neither the workstation process environment nor a target shell's ambient environment is part of
`ResourceContext`. An additional lookup source must specify its identity, timing, missing-value
behavior, sensitivity, and inspection behavior. Login and interactive shell startup can affect
target values, and offline inspection cannot observe them.

## Implementation map

- `cli/agentworks/env/identity.py`: context model and identity producers.
- `cli/agentworks/env/entry.py`: literal and secret entry types.
- `cli/agentworks/env/merge.py` and `compose.py`: scope precedence and execution env rendering.
- `cli/agentworks/env/show.py`: offline effective-env inspection.
- `cli/agentworks/sessions/manager/_env.py`: current session string substitution.
- `cli/agentworks/capabilities/harness_integration/base.py`: literal argv protection.
- `cli/agentworks/harness_setup/inputs.py`: setup env composition.

See the [CLI session-template reference](../../cli/command-reference.md#session-templates) for the
operator-facing shell integration configuration.
