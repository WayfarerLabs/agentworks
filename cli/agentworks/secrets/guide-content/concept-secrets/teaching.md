Use the implementation inventory to understand available source implementations. A configured
source's readiness predicts whether it can participate; preview never proves that a particular
secret resolves.

Use `agw secret describe NAME` for non-resolving prediction. Use `agw secret verify NAME...` only
after consent for a real batch proof; it reports one value-free typed outcome per unique name and
exits nonzero if any outcome is not resolved. Verification refuses interactive sources by default.
Ask separately before adding `--allow-interaction`, then resolve through the ordinary secure input
boundary and its injected interaction policy. Never inspect a source broadly to find the value.
