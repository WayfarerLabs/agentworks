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
`agw secret describe NAME` for a non-resolving prediction.

The optional `onepassword` plugin supplies the shipped non-synthesized backend. Enable that system
plugin, declare a source that selects `onepassword`, and map each secret to a scalar `op://`
reference. `agw resource explain secret-backend/onepassword` shows the current fields; the
[Secret Sources README](../README.md#declaring-a-source) gives one complete example.

Use `agw secret verify NAME...` only when resolving those exact names is covered by the operator's
instruction. It reports value-free outcomes and refuses interactive sources by default. Before
adding `--allow-interaction`, state the possible prompt, biometric, or renewed-authentication
effect. If declined, leave the secret unverified and do not inspect its sources broadly.

Never display, log, or retain a resolved secret. Pass it only to the authorized sink. Secret values
may contain structured multiline text; source verification proves resolvability, not suitability for
every sink. Each sink enforces its own shape.
