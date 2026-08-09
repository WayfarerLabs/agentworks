# Secret Sources

`secret-source` is the declarable resource that participates in resolution. Each source has an
operator-chosen name and selects one registered `secret-backend` capability in `spec.backend`.
Backends remain read-only implementation rows under
[`capabilities/secret_backend`](../capabilities/secret_backend/README.md); sources are configured
instances of those implementations.

The distinction matters when one backend needs several configurations. Two sources can both select
`onepassword` while using different accounts, timeouts, and precedence positions. A secret's
`backend_mappings` keys are source names, and `[secret_config].backends` keeps its existing spelling
but contains source names in resolution order.

## Simple defaults

Agentworks synthesizes `env-var` and `prompt` sources. With no extra configuration, the chain stays
`["env-var", "prompt"]` and behaves as before:

- `env-var` reads `AW_SECRET_<UPPER_SNAKE_NAME>`, or the environment variable named by that secret's
  `backend_mappings.env-var` scalar. An unset variable is a soft miss.
- `prompt` asks through an explicit caller-owned interaction broker. It has no mapping vocabulary;
  `backend_mappings.prompt: false` opts one secret out.

The first source that returns a value wins. Duplicate requested names collapse in first-encounter
order, every attempted source receives one ordered batch, and a hard provider failure or timeout
halts only the secrets attributed to that attempted batch.

## Declaring a source

Additional sources are YAML resources under `~/.config/agentworks/resources/`:

```yaml
apiVersion: agentworks/v1
kind: secret-source
metadata:
  name: work-op
spec:
  backend:
    name: onepassword
    account: work.example.com
    timeout: 30
```

Enable the `onepassword` system plugin as well. Its permanent per-secret mapping is a scalar native
reference:

```yaml
apiVersion: agentworks/v1
kind: secret
metadata:
  name: npm-token
  description: npm registry token
spec:
  backend_mappings:
    work-op: op://Engineering/npm/token
```

Then place `work-op` wherever it belongs in `[secret_config].backends`. A configured backend name
such as `onepassword` is not treated as a source alias in 0.14. The synthesized `env-var` and
`prompt` source names remain valid unchanged. An unknown source name that exactly matches a
configured backend produces a hard error with the source manifest and mapping rewrite; there is no
compatibility source or legacy parser. When rewriting the old OnePassword mapping table, move its
account to the source; the optional source timeout is new and defaults to 30 seconds.

## Runtime contract

Resolution opens one lazy client for each source it actually attempts. The factory is resource-free;
the context enters, prepares one batch, resolves once, and closes before the next source begins.
OnePassword owns a positive external-operation timeout and applies the shrinking remaining budget to
each `op read`. Env-var and prompt perform no non-human blocking I/O and declare no timeout.

Results use five value-free categories: `resolved`, `unavailable`, `refused-interaction`, `timeout`,
and `resolution-failure`. Resolved values live only in a private operation batch and the existing
operation cache. Outcomes, identifiers, errors, warnings, logs, and render inputs never contain a
value. NUL, carriage return, and newline are rejected before a value can become resolved.

Complete command resolution checks for doomed secrets before every allowed interactive turn, so it
does not prompt or trigger biometric authentication when another requested secret is already known
to fail. The explicit `agw env show --resolve` partial-reveal path may still resolve independent
secrets; list, describe, doctor, schema, guide, and completion never do. Cleanup always runs after
entry; cleanup failure warns with fixed source-only text and never masks the primary result,
timeout, or interruption.

Use `agw secret list` and `agw secret describe NAME` for value-free inspection. Use
`agw secret verify NAME...` for one real batch proof. It deduplicates names in first-written order,
resolves the batch once, and emits one value-free row per unique name with category, source, safe
identifier, detail, and remediation. It exits 1 after rendering if any row is not `resolved`.
Verification refuses interaction by default. Add `--allow-interaction` only with operator consent
for prompts, biometric checks, or renewed authentication; the opt-in is incompatible with global
`--non-interactive`.
