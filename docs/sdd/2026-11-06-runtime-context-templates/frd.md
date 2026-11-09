# Runtime context templates: requirements

Status: operator-approved scope. The feature uses `${agw:...}` throughout. The full SDD,
implementation, permanent documentation, and validation belong to one PR.

## Purpose

Reusable env definitions can refer to the consuming Linux user's managed home and other resource
context without hardcoding a username. The same interpolation contract serves env values and
operator-authored workload commands.

## FR1. Context vocabulary

Support `user_home_dir`, `user_name`, `vm_name`, `agent_name`, `workspace_name`, `workspace_dir`,
and `session_name`. The consuming user is the selected workload or setup identity, independent of
which scope declares the expression and which account carries an administrative transport hop.

```yaml
env:
  TOOL_CONFIG_DIR: "${agw:user_home_dir}/.config/tool"
  PROJECT_DIR: "${agw:workspace_dir}"
```

Use the actual Linux username for `user_name`, the managed home of that username for
`user_home_dir`, and the agent resource name for `agent_name`. Optional resource context is
available only where the corresponding resource applies.

## FR2. Reserved syntax

A context reference is `${agw:NAME}`. Only this namespace belongs to Agentworks. Unknown names,
unavailable context, and malformed references within it fail with field context. Literal escaping
uses `$${agw:NAME}` to produce `${agw:NAME}` without lookup. Escaping is limited to the reserved
namespace; ordinary dollar signs and brace text are unchanged.

Render once. Inserted values are data and are never interpreted again as template expressions. Shell
expressions and Go templates pass through unchanged. Do not implement expressions, filters, command
evaluation, ambient environment lookup, or references between managed env entries.

## FR3. Env scope and precedence

Support plaintext env values from VM, workspace, admin, agent, and session templates and stored
instance overlays. Resolve inheritance and overlays, merge scopes, then render winning entries
against the consuming context. Preserve precedence: VM, workspace, agent or admin, session, with
later scopes winning. Agent and admin are alternatives for the consuming Linux user.

A discarded entry must not fail runtime rendering because it references unavailable context.
Identity env precedence remains enforced. Env names are literal and are not interpolated.

## FR4. Opaque secrets and literal workload input

Secret reference names and resolved secret contents are never interpolated or unescaped. This
includes text resembling `${agw:NAME}` and `$${agw:NAME}`. Existing consumer validation still
applies to secret values. Switching an entry from plaintext to a secret changes its interpretation
from a template to literal data; document and test that boundary.

Native harness prompts, profile names, and other workload data likewise retain their literal
content. Explicit command fields and the integration's designated extra arguments use the shared
context renderer.

## FR5. Commands and quoting

Use one grammar and context catalogue across supported env and command consumers. Env values are
literal transport data; commands are shell source. Interpolation does not automatically quote
arbitrary placement inside shell source. Commands needing an arbitrary workspace path can use
`"$AGENTWORKS_WORKSPACE_DIR"`, preserving spaces and apostrophes as part of one argument.

## FR6. Inspection and lifecycle

Offline `env show` must render the same winning plaintext values as execution for equivalent
context, preserving scope provenance and default secret redaction. It must not connect to a VM or
activate it. Secret resolution remains explicit.

Recompute values when preparing shell/exec, setup, or session create/start/restart. Existing
processes do not acquire changed values automatically. Validate references before prompts and
mutations wherever context is known, and always before restart tears down the existing runtime.
Newly planned users must be supported as soon as their identities are determined.

Console extra shells have workspace and selected-user context, without session context. Setup uses
the actual setup identity; workspace setup runs as the VM admin.

## FR7. Permanent documentation and delivery

`docs/arch/runtime-context.md` is the permanent standalone reference for vocabulary, availability,
grammar, escaping, expansion order, consumer adoption, and quoting. Keep CLI/schema guidance and
applicable sample/guide collateral consistent in the same PR. Document the destination; explain
syntax choice briefly in the prior-art rationale.

Deliver the feature and full SDD in PR #867. Mark the completed implementation ready for the
standard review and integration-testing pipeline. The operator authorizes up to three final PR
review iterations; wait for the integration report before a subsequent round, allowing up to four
hours for its arrival.

## Operator rulings

### 2026-11-08: command checks and transport scope

The following authenticated operator rulings govern FR5 and FR6 and the transport scope of this
feature. They are recorded verbatim.

> Yeah, agreed that the current behavior is fine. And to be clear, we shouldn't be validating the
> actual command. Rather just that it renders and that required_commands are present. The command
> itself just need to be executed to see if it works.

> For #1, please just file an issue. We're completely reworking the ssh and transport stack. I think
> this is already addressed but I'll have the dev look at your issue to make sure. We should not do
> anything special here.

<!-- cspell:ignore tbh -->

The operator subsequently requested earlier shell template preflight:

> And yeah, tbh, we should "preflight" the shell integration prior to secret resolution. That would
> at least identify rendering failures before secret resolution, right?
