# Runtime context templates: interpolation contract

Status: proposed. This contract covers context placeholders only. The [FRD](frd.md) recommends
omitting ambient lookup and managed env cross-references, subject to draft acceptance.

## Grammar

- A placeholder is exactly `{{NAME}}`, where `NAME` is an ASCII identifier matching
  `[A-Za-z_][A-Za-z0-9_]*` and belongs to the documented context catalogue.
- Whitespace inside a placeholder is rejected rather than silently normalized.
- `{{{{` emits a literal `{{`. Consequently `{{{{session_name}}` emits the literal text
  `{{session_name}}`. The escaped opening delimiter does not start a placeholder.
- Parse left to right, recognizing escaped delimiters before opening a placeholder.
- Closing braces outside an active placeholder and lone braces are ordinary text. Only an unescaped
  opening `{{` starts template syntax; a malformed or unclosed placeholder raises `ValidationError`
  with the owning field location. Ordinary nested JSON ending in `}}` is literal.
- Replacement strings are appended as data, never parsed again. Dollar signs, backticks, and `~`
  have no interpolation meaning. There is no expression evaluation or dependency on a third-party
  template library.

The malformed-opening rule is a deliberate tightening of the current regex, which leaves many
unmatched opening sequences unchanged. The migration must disclose and test this behavior, not call
it wholly backward compatible. Existing valid two-name placeholders keep working.

## Pure rendering boundary

The shared module receives explicit context values and knows the supported catalogue. Unknown names
and known names absent from the supplied context are separate errors. Errors identify the name and
field, but do not include the entire input, replacement values, or rendered command.

The implementation may use a small scanner and ordinary mappings. A separate AST, plugin registry,
resolver hierarchy, or generic expression evaluator is unnecessary. The same scanner can collect
references for preflight without requiring replacement values or exposing secrets.

## Env rendering

Remove session-only early expansion. Select winning entries first, then render plaintext through one
function used by execution and display. Preserve `EnvEntry`'s literal-versus-secret distinction and
original declarations; do not persist rendered values over template text. A secret entry is never a
template, even if its resolved string contains braces.

Validate rendered plaintext at the actual env transport boundary alongside existing env safety
handling. A valid placeholder expression does not make arbitrary replacement text safe for every
destination. Missing context is a preflight error where context is already known.

## Commands

Preserve the current contract that commands are authored shell source and callers quote path
placeholders appropriately, for example `tool --config '{{user_home_dir}}/.config/tool'` under the
managed-home naming convention. Do not add a universal `shlex.quote` around substitutions: quoting
an arbitrary fragment inside an already quoted shell program changes its meaning.

The same parsed expression rendered to an argv element in a future consumer yields plain text; that
consumer quotes the final argv element once if it must serialize a shell command. Current
literal-field protection shields opening delimiters; leaving closing braces literal preserves that
boundary. Verify repeated braces and escaped openers against `quote_literal_argv`, not only the two
valid legacy names.

## Review vectors

| Boundary     | Cases                                                                                                                                 |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| Parser       | Legacy names; new names; adjacent placeholders; escapes; malformed/unclosed delimiters; unknown names; non-ASCII and whitespace forms |
| Replacement  | Empty available value; inserted brace text is not scanned again; literal dollar signs and command syntax                              |
| Context      | Agent name differs from Linux user; admin session; workspace-free shell; console pane without session; setup identities               |
| Merge        | Child and instance override; higher-scope override; losing unavailable reference; literal replaces secret and secret replaces literal |
| Secrets      | Secret name and secret value remain literal; reveal still explicit; unavailable secret remains existing typed outcome                 |
| Inspection   | Execution/display plaintext parity for equivalent context; provenance retained; no transport access                                   |
| Lifecycle    | Create/start/restart coverage; invalid winning reference rejected before replacement teardown; newly planned agent context            |
| Harness data | Native prompt/profile literals and deliberately expanded extra args; quotes, spaces, repeated braces, dollars, and newlines           |

Use behavior assertions, structured rows, recorded transport calls, and existing local test
fixtures. Do not pin authored help, documentation wording, or source layout in tests.
