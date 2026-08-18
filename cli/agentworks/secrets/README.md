# Secret Sources

`secret-source` is the declarable resource that participates in resolution. Each source has an
operator-chosen name and selects one registered `secret-backend` capability in `spec.backend`.
Backends remain read-only implementation rows under
[`capabilities/secret_backend`](../capabilities/secret_backend/README.md); sources are configured
instances of those implementations.

The distinction matters when one backend needs several configurations. Two sources can both select
`onepassword` while using different accounts, timeouts, and precedence positions. A secret's
`backend_mappings` keys are source names, and `[secret_config].sources` contains source names in
resolution order.

## Simple defaults

Agentworks synthesizes `env-var` and `prompt` sources. With no extra configuration, the chain stays
`["env-var", "prompt"]` and behaves as before:

- `env-var` reads `AW_SECRET_<UPPER_SNAKE_NAME>`, or the environment variable named by that secret's
  `backend_mappings.env-var` scalar. An unset variable is a soft miss. A set value is returned
  exactly, including terminal carriage returns or line feeds.
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

Then place `work-op` wherever it belongs in `[secret_config].sources`. A configured backend name
such as `onepassword` is not treated as a source alias in 0.14. The synthesized `env-var` and
`prompt` source names remain valid unchanged. An unknown source name that exactly matches a
configured backend produces a hard error with the source manifest and mapping rewrite; there is no
compatibility source or legacy parser. When rewriting the old OnePassword mapping table, move its
account to the source; the optional source timeout is new and defaults to 30 seconds.

## Runtime contract

Resolution opens one lazy client for each source it actually attempts. The factory is resource-free;
the context enters, prepares one batch, resolves once, and closes before the next source begins.
`agentworks.secrets.resolve.OutputInteractionBroker` is the module-owned public CLI broker; the
source orchestrator passes it only to the terminal-channel backend (`prompt`) and passes `None` to
every other backend factory. OnePassword owns a positive external-operation timeout and applies the
shrinking remaining budget to each `op read`. Env-var and prompt perform no non-human blocking I/O
and declare no timeout.

Results use five value-free categories: `resolved`, `unavailable`, `refused-non-interactive`,
`timeout`, and `resolution-failure`. A not-ready outcome retains only bounded remediation metadata;
a disabled system-plugin backend is attributed by plugin name and rendered with a fixed enablement
action. Resolved values live only in a private operation batch and the existing operation cache.
Outcomes, identifiers, errors, warnings, and logs never contain a value. NUL is rejected before a
value can become resolved. Carriage returns and line feeds remain ordinary opaque string content, so
a structured credential can retain the formatting and terminal newline it had at the source.

Syntax constraints belong to the consumer. Environment injection and `agw env show --resolve`, Git
authenticated probes and credential lines, Proxmox API headers, and Tailscale stdin joins each
require one logical line. Those consumers reject CR, LF, or NUL with fixed, value-free diagnostics
immediately after delivery and again at final material sinks where appropriate. SDK consumers such
as GCP service-account authentication receive the opaque multiline string unchanged. A successful
`agw secret verify` proves only that the source contract resolved the value; it does not prove that
every narrower consumer syntax can use it.

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
Resolution allows sources that may block on a human by default; `--non-interactive` is the explicit
switch that refuses them. `secret verify --allow-interaction` is a deprecated no-op kept for one
release (interaction is already allowed by default); it warns unless `--no-deprecations` is set.

## Interaction channels

Each backend declares a static `interaction_channel` fact on its class (`none`, `terminal`, or
`out-of-band`; see `InteractionChannel` in
[`capabilities/secret_backend/base.py`](../capabilities/secret_backend/base.py)). `none` (`env-var`)
never blocks on a human. `terminal` (`prompt`) collects input through this process's own terminal,
so the resolver additionally requires a terminal to be available
(`output.terminal_prompt_available()`); without one it skips the source into fall-through rather
than failing the whole batch. `out-of-band` (`onepassword`) may trigger an approval outside this
process (a biometric or re-auth prompt on the operator's desktop), so it needs consent only, no
terminal: the operator configuring an approval-prompting source is the consent, and the source's own
resolution timeout bounds the wait.
