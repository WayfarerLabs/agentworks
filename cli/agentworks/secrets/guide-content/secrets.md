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

Use `agw secret verify NAME...` only when resolving those exact names is covered by the operator's
instruction. It reports value-free outcomes. Resolution may block on a human by default: a
terminal-channel source (`prompt`) prompts through this process's own terminal (skipped instead of
hanging where none is available), and an out-of-band source (e.g. `onepassword`) may trigger an
approval prompt on the operator's desktop outside this process. Before running a command that
resolves such a secret, state that possible effect: which source, what kind of prompt or approval,
and the expected result. If declined, leave the secret unverified and do not inspect its sources
broadly. `--non-interactive` refuses every source that could block on a human, for a caller that
wants a hard failure instead.

Never display, log, or retain a resolved secret. Pass it only to the authorized sink. Never work
around a refusal or a skip by exporting a secret's value into the process environment (for example
`AW_SECRET_<NAME>=$(op read ...)`): that bypasses the source the operator configured and puts a
plaintext value into the environment, the exact exposure the named-secret system exists to avoid.
Secret values may contain structured multiline text; source verification proves resolvability, not
suitability for every sink. Each sink enforces its own shape.
