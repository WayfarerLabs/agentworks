# Runtime context templates: architecture

The [requirements](frd.md) govern this design. A shared pure renderer combines explicit resource
context with the reserved `${agw:...}` namespace. It has no I/O, expression engine, environment
lookup, resolver registry, or secret resolution responsibility.

## Context catalogue

Use the existing `ResourceContext` as the source of runtime identity. Convert it to the finite
placeholder catalogue without creating another identity model. Preparation may supply already-known
planned facts before a full runtime context exists; both paths use the same context-values producer
and catalogue.

| Name             | Value                            | Availability                         |
| ---------------- | -------------------------------- | ------------------------------------ |
| `user_name`      | Actual consuming Linux username  | Every supported env composition      |
| `user_home_dir`  | Managed home of that username    | Every supported env composition      |
| `vm_name`        | Selected or planned VM name      | Every supported env composition      |
| `agent_name`     | Selected agent resource name     | Agent execution and setup            |
| `workspace_name` | Selected workspace resource name | Workspace-rooted execution and setup |
| `workspace_dir`  | Managed workspace root           | Same as `workspace_name`             |
| `session_name`   | Selected or planned session name | Session workload execution           |

The managed-home helper implements `/home/<Linux username>` and is shared with initialization and
setup. Derive the placeholder from `ResourceContext.user`; do not duplicate home state or consult
workstation `HOME`. Arbitrary account-home relocation is outside this managed model.

The consumer's context determines availability. A VM-level env definition can refer to a user's home
and render differently for different consuming users. A winning `session_name` reference is
unavailable during VM setup or a standalone agent shell.

## Composition

1. Decode declarations and resolve template inheritance and stored instance overlays.
2. Determine the consuming identity and resource context.
3. Merge applicable env scopes in precedence order and discard collisions with applicable managed
   identity before validation or secret collection.
4. Render each winning plaintext entry once; preserve secret references as literal entries.
5. Fill secrets from the command's existing eager resolution and enforce existing consumer safety.
6. Add managed identity values and deliver literal env strings through the transport.

A common plaintext-rendering path serves execution and offline inspection. It must be usable without
resolving secrets, so redacted inspection and context validation do not require secret values. Keep
winning-scope provenance independent of rendering.

Check winning env references before workload secret prompts and mutations when the invocation knows
its identity. Bind the planned context and explicit launch intent before the existing preflight.
Shell preflight checks that the selected command field renders, including when the required-command
probe defers for a pending target. The check precedes workload/env secret resolution; the existing
VM activation gate may resolve its own credentials before remote preflight.

The existing readiness path checks declared command availability. These checks do not predict
command behavior; execution determines that result. Each integration renders its declared template
fields while selecting its launch command, before launch or destructive replacement. Core consumes
returned commands directly. No separate generic validation hook is needed. Deriving context and
checking rendering require no target connection.

## Invocation coverage

| Invocation                   | Consuming user                     | Additional context                              |
| ---------------------------- | ---------------------------------- | ----------------------------------------------- |
| VM shell/exec                | VM admin                           | Optional selected workspace                     |
| Agent shell/exec             | Agent's Linux user                 | Agent and optional authorized workspace         |
| Session create/start/restart | Selected admin or agent            | Workspace, session, and agent when applicable   |
| Console extra shell          | Pane's selected admin or agent     | Workspace and agent when applicable; no session |
| VM/admin setup               | VM admin                           | No workspace, agent, or session                 |
| Agent setup                  | Agent's Linux user                 | Agent; no workspace or session                  |
| Workspace setup              | VM admin                           | Workspace; no agent or session                  |
| Offline `env show`           | Selected agent, otherwise VM admin | Selected/inferred workspace and session         |

Native transports retain their documented env-delivery limitations. The operator directed tracking
the inherited SSH literal-value observations in issue #873 for the transport rewrite, with no SSH
special case in this feature. Setup and runtime use the same managed identity semantics.

## Literal data and shell source

Only `${agw:` starts Agentworks interpolation. Dollar signs and Go-template braces otherwise belong
to their downstream consumer. The renderer is strict inside its own namespace and does not scan
replacement values again.

Env values are literal strings. Shell commands are authored source, so quoting depends on where text
appears. In command source, use `"$AGENTWORKS_WORKSPACE_DIR"` for arbitrary workspace paths; raw
substitution between single quotes cannot represent an embedded apostrophe safely.

Bind the invocation context before the integration selects its launch command. Shell renders its
selected command field; native integrations render structured extra arguments as data before quoting
them. Prompts, profiles, and opaque workload fields are literal argv data. Core performs no further
interpolation of returned commands. Future consumers render only their declared template fields,
before serializing structured arguments into shell source.

Secret values bypass both interpolation and unescaping. This is a consumer distinction, not a
request for another template syntax. Do not infer a context source from exported identity env or
build dependencies between arbitrary managed env entries.

## Permanent reference and delivery

The standalone reference is `docs/arch/runtime-context.md`; no permanent consumer depends on the SDD
directory. The implementation, full SDD, documentation, and evidence remain in one PR. Local
project, complexity, and correctness reviews precede the ready handoff. Subsequent iterations wait
for the integration report before beginning a new correction round.
