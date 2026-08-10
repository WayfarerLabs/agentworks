Secret resources declare names and source mappings. A `secret-source` is a configured instance that
selects one `secret-backend` implementation. Settings and every `backend_mappings` key name sources,
not backend implementations. Registry inspection surfaces expose declarations; the guide's global
implementation inventory exposes capability readiness. Neither exposes resolved values.

Agentworks synthesizes the `env-var` and `prompt` sources, so the simple case needs no source
manifest. The environment source derives `AW_SECRET_<UPPER_SNAKE_CASE>` unless a secret mapping
overrides it. The prompt source has no static identifier.
