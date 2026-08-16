`agw resource list --kind secret-backend --include-disabled --output json` includes capability
implementations from every domain, not configured secret sources or their order. Use those rows to
find the implementations a source can select. A configured source's readiness predicts whether it
can participate; preview never proves that a particular secret resolves.

Use `agw resource sample secret-source` to start a source manifest and
`agw resource describe-kind secret-backend/NAME` to inspect an implementation contract. For
OnePassword, declare a source that selects the `onepassword` backend and holds its account and
optional timeout; each secret maps that source name directly to one scalar `op://` reference. The
configured backend name `onepassword` is not a source alias. The synthesized source names `env-var`
and `prompt` remain valid directly in `[secret_config].sources`.

Use `agw secret describe NAME` for non-resolving prediction. Use `agw secret verify NAME...` for a
real batch proof when the exact names and value-free resolution class are inside the current
envelope; it reports one value-free typed outcome per unique name and exits nonzero if any outcome
is not resolved. Verification refuses interactive sources by default. Add `--allow-interaction` only
when prompt, biometric, or renewed-authentication impact is already explicit in the operator's
instruction or after one resolving decision. Then resolve through the ordinary secure input boundary
and its injected interaction policy. Never inspect a source broadly to find the value.

Secret resolution accepts structured multiline text without compaction or alternate encoding. It
preserves carriage returns, line feeds, and terminal line endings exactly; NUL is the one globally
rejected string value. A source-level verification therefore proves resolvability, not suitability
for every sink. Environment injection and reveal, Git credential headers and files, Proxmox API
headers, and Tailscale stdin joins each require a single logical line and reject incompatible values
at their own boundary. SDK consumers that support structured text, including GCP service-account
authentication, receive the opaque value unchanged.
