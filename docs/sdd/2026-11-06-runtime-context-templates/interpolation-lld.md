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
  unchanged. No other syntax is claimed by Agentworks.

The renderer receives authored text, explicit context, and a field location. Errors identify the
field and offending name or syntax category, without including the whole input, replacement values,
or rendered commands. The implementation needs a small scanner or bounded matcher, not an AST or a
general template engine.

## Env consumer

Select winning entries after inheritance, instance overlays, and scope merging. Render plaintext
through one function shared by execution and display. Keep original desired declarations intact;
never persist rendered values over their expressions. Skip interpolation and unescaping for both
secret names and secret contents.

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

Literal argv construction must shield `${agw:` before a returned command passes through the
renderer. Shell quoting alone does not hide syntax from a textual renderer. Preserve exact native
harness data and its argument boundaries. Validate returned command source after integration launch
selection and before a restart destroys the current runtime. Env context is validated before
boundary prompts when its facts are already known; command generation does not require a separate
preflight interface.

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
