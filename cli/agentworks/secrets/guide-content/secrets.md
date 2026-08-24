---
description: Configure and verify named secrets without exposing their values.
index-order: 70
---

# Secret handling

Secret resources declare names and source mappings. A `secret-source` selects one `secret-backend`
implementation. Settings and `backend_mappings` name sources, not backend implementations. Registry
inspection exposes declarations and readiness, never resolved values.

Agentworks synthesizes the `env-var` and `prompt` sources, so the simple case needs no source
manifest. Use `agw resource sample secret-source` to start a source manifest and
`agw resource explain secret-backend/NAME` for its implementation contract. Use
`agw secret describe NAME` for a provider-aware, value-free preview.

The optional `onepassword` plugin supplies the shipped non-synthesized backend. Enable that system
plugin, declare a source that selects `onepassword`, and map each secret to a scalar `op://`
reference. `agw resource explain secret-backend/onepassword` shows the current fields; the
[Secret Sources README](../README.md#declaring-a-source) gives one complete example.

Both `agw secret describe NAME` and `agw secret verify NAME...` default to no operator impact. A
backend may still read a provider value and safely discard it when it can do so without operator
action. Use either command's `--allow-interaction` only when previewing those exact names is covered
by the operator's instruction, and state the possible prompt, biometric, app, browser, device, or
renewed-authentication effect first. The flag permits backend-classified operator action and makes
the answer definitive; it never returns the value. If declined, keep the no-impact result and do not
inspect the sources broadly.

Global `--non-interactive` is independent: it only disables TTY interaction, even if a terminal is
present. It does not disable presentation or out-of-band provider work. It is not an unattended
fail-fast mode, so app authentication may request approval and wait for the source timeout. Use
`env-var` or provider authentication known to be unattended, such as supported 1Password service
account or Connect credentials, for unattended work.

Never display, log, or retain a resolved secret. Pass it only to the authorized sink. Secret values
may contain structured multiline text; source verification proves resolvability, not suitability for
every sink. Each sink enforces its own shape.
