The implementation inventory is global: it includes capability implementations from every domain,
not configured secret sources or their order. For this topic, use its `secret-backend` rows to find
the implementations a source can select. A configured source's readiness predicts whether it can
participate; preview never proves that a particular secret resolves.

Use `agw resource sample secret-source` to start a source manifest and
`agw resource describe-kind secret-backend/NAME` to inspect an implementation contract. For
OnePassword, declare a source that selects the `onepassword` backend and holds its account and
optional timeout; each secret maps that source name directly to one scalar `op://` reference. The
configured backend name `onepassword` is not a source alias. The synthesized source names `env-var`
and `prompt` remain valid directly in `[secret_config].backends`.

Use `agw secret describe NAME` for non-resolving prediction. Use `agw secret verify NAME...` only
after consent for a real batch proof; it reports one value-free typed outcome per unique name and
exits nonzero if any outcome is not resolved. Verification refuses interactive sources by default.
Ask separately before adding `--allow-interaction`, then resolve through the ordinary secure input
boundary and its injected interaction policy. Never inspect a source broadly to find the value.
