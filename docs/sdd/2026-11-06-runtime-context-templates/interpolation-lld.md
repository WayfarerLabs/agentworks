# Runtime context templates: interpolation contract

This contract implements the [requirements](frd.md) using explicit resource context.

## Grammar

- A reference is `${agw:NAME}`, with `NAME` matching ASCII `[A-Za-z_][A-Za-z0-9_]*`.
- Only `${agw:` reserves syntax. A malformed or unclosed reference within that namespace is a
  `ValidationError`; whitespace is not silently normalized.
- Unknown names and supported names unavailable to the invocation are distinct errors.
- `$${agw:NAME}` emits literal `${agw:NAME}` without a value lookup. Escaping applies only to the
  reserved namespace. An escaped name does not have to exist in the catalogue.
- Parse left to right, recognizing a namespace escape before a reference. Append replacements as
  data without scanning them again.
- `$HOME`, `${HOME}`, `${PATH:-default}`, `$$`, ordinary dollar signs, and Go-template text remain
  unchanged except where a dollar immediately precedes a reserved reference: `$$${agw:user_name}`
  becomes `$${agw:user_name}` through the namespace escape. No other syntax is claimed by
  Agentworks.

The renderer receives authored text, explicit context, and a field location. Errors identify the
field and offending name or syntax category, without including the whole input, replacement values,
or rendered commands. The implementation needs a small scanner or bounded matcher, not an AST or a
general template engine.

## Env consumer

Select winning entries after inheritance, instance overlays, scope merging, and applicable managed
identity precedence. Discard ignored identity collisions before validation and secret collection.
Render plaintext through one function shared by execution and display. Keep original desired
declarations intact; never persist rendered values over their expressions. Skip interpolation and
unescaping for both secret names and secret contents.

Context validation works without secret values. Unknown or unavailable references in winning entries
fail before operations requiring them; losing entries are not rendered. Preserve transport-specific
value validation after rendering. A syntactically valid placeholder does not make its replacement
suitable for every destination.

The same authored text can intentionally have different results as plaintext and as a secret.
Plaintext `$${agw:user_name}` emits `${agw:user_name}`; secret content `$${agw:user_name}` retains
both dollar signs. This distinction follows the declared entry type.

## Command consumer

Commands are shell programs. Interpolation is textual substitution, not automatic quoting. A managed
home may be used in `tool --config '${agw:user_home_dir}/.config/tool'` under the managed Linux
username convention. Use `"$AGENTWORKS_WORKSPACE_DIR"` for an arbitrary workspace path, including
spaces or apostrophes.

Bind the selected context to the integration before launch selection. Shell renders the selected
`command` or `resume_command` field against that context. Native integrations render structured
extra arguments as individual data values before quoting them with `shlex.quote`; other native
fields remain literal argv data. Core consumes the returned command without further interpolation.
Preserve exact native harness data and its argument boundaries. Bind launch intent and planned
context before preflight. Shell preflight checks rendering of the selected field before workload/env
secret resolution, even when a pending target defers the required-command probe. Startup uses the
same selection/rendering helper on the declaration, without caching its rendered value. The existing
VM activation gate can resolve its own credentials before remote preflight.

Declared `required_commands` use the existing target availability probe. Template renderability and
declared-command availability are the checks; command success is established by execution. Native
structured arguments render during launch selection before destructive replacement. Env context is
validated before workload boundary prompts when its facts are known; no separate generic preflight
interface is introduced.

## Behavioral vectors

| Boundary      | Cases                                                                                                                                                       |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Parser        | All seven names, adjacent references, escapes, unknown names, missing context, malformed/unclosed namespaced forms, whitespace and non-ASCII names          |
| Ordinary text | Shell parameters and dollars, Go fields/functions/control blocks, JSON, unmatched unrelated braces                                                          |
| Replacement   | Empty available value, inserted namespace text not scanned again, quotes and dollar signs remain data                                                       |
| Context       | Different agent/resource and Linux-user names, admin execution, workspace-free shell, console shell without session, setup identities, newly planned agents |
| Merge         | Inheritance and instance overlays, higher scopes win, losing unavailable reference, literal/secret replacement in both directions                           |
| Secrets       | Names and values remain literal, escape-looking text unchanged, explicit reveal and existing safety guards                                                  |
| Inspection    | Execution/display plaintext parity, winning provenance, default redaction, no transport access                                                              |
| Lifecycle     | Create/start/restart, invalid command/env reference leaves existing runtime intact                                                                          |
| Harness       | Literal prompts/profile fields, intentionally interpolated extra args, shell argument boundaries                                                            |

Test behavior with structured rows and recorded calls. Do not assert on authored help, documentation
wording, or source layout.
