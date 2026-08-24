# Upgrading to 0.15

What an operator needs to know when crossing the 0.15 release boundary: secret preview became
provider-aware, so `agw doctor`, `agw secret describe`, and preflight now do real backend work where
they used to be pure and local, and `agw secret verify`'s output vocabulary changed wholesale. The
generated release note for this change is scoped to secret-backend plugin authors (it describes the
new `contract_version` a backend must declare); this guide covers what changes for the operator
running the shipped CLI. It is a companion to
[`cli/agentworks/secrets/README.md`](../../cli/agentworks/secrets/README.md), which describes the
preview and resolution contract as it stands and assumes none of this is in flight.

**This guide is release-scoped.** It exists only to carry hosts from 0.14 to 0.15, and it is deleted
outright a release or two after 0.15 ships. Nothing here describes permanent behavior; for that,
read the secrets README above.

## `agw doctor` can now exit nonzero from a provider or network condition

Pre-0.15, a secret check was `ok` or `warn`; it could never fail the run, because doctor's preview
never contacted a secret provider. Now a secret check can be `FAIL`, and `agw doctor` exits 1
whenever any check fails. A CI job that gates on `agw doctor`'s exit code can turn red from a
provider hiccup with no code change on your side.

This happens through the closed preview-status mapping: `available` and `indeterminate` are OK,
`missing` and `blocked` are WARN, and `failed` is FAIL. `failed` is real: for the built-in `env-var`
and `prompt` backends it only fires on a malformed value (a NUL byte), but for a provider backend
like `onepassword` it fires on the provider's own failure modes when doctor's zero-impact preview is
actually able to reach the provider (see the next section for when that happens):
sign-out/authentication failure, the referenced item or field not found, connectivity or timeout, or
an unrecognized non-zero exit from `op`.

**What to do:** treat a new `FAIL` in the Secrets group as a real signal, not noise. The row names
the secret and, for JSON output, the check carries a `secret_preview` object with a closed `reason`.
Run `agw secret describe NAME --allow-interaction` for a definitive read on what the provider is
doing. If your CI only needs a pass/fail signal, the exit code is still exactly that; if it parses
the human table, the `[FAIL]` label and the `Results: ... fail` line are unchanged in shape.

## `agw doctor` notes an unset prompt secret without warning

Pre-0.15, doctor's prediction for an unset secret falling through to the default `prompt` source
reported `ok: would attempt via prompt`, regardless of whether a TTY was actually available.
Doctor's zero-impact preview now asks the `prompt` backend for real, and at zero operator impact
prompt cannot give a definite answer because asking would be the operator action. It returns
`indeterminate` when a TTY is available and keeps the row OK with a compact
`indeterminate/operator-impact-limited; source=prompt` note. With no usable TTY it returns
`blocked/tty-unavailable; source=prompt`, which is a warning because command-time prompting is not
possible in that environment.

**What to do:** no action is needed for an indeterminate row. Doctor deliberately avoids operator
interaction, and that uncertainty is expected. A blocked row still means the current environment
cannot use the prompt fallback; configure the value in an earlier non-TTY source if the command must
run there. Doctor has no `--allow-interaction` flag to force a definitive answer.

## `agw doctor`, `agw secret describe`, and preflight now perform provider I/O

Pre-0.15, none of these three ever opened a secret-backend client: doctor and preflight's prediction
was documented as "never reads a value or opens a source client," and `secret describe` was
documented as "no prompting and no resolution for display." All three now call the shared one shared
preview path, which opens a real backend client per active source and calls its `preview()` method.

Each caller's default operator impact:

| Caller                       | Default impact | Can raise it?                         |
| ---------------------------- | -------------- | ------------------------------------- |
| `agw doctor`                 | `NONE` (fixed) | no                                    |
| preflight (before every run) | `NONE` (fixed) | no                                    |
| `agw secret describe`        | `NONE`         | yes, `--allow-interaction` (new flag) |
| `agw secret verify`          | `NONE`         | yes, `--allow-interaction`            |

`NONE` means "no operator action required," not "no I/O." A backend is free to do work that needs no
operator action: `env-var` reads and discards a value to validate it; `onepassword` runs a real
`op read` at `NONE` impact whenever it already knows authentication is unattended (an
`OP_SERVICE_ACCOUNT_TOKEN` or `OP_CONNECT_HOST`/`OP_CONNECT_TOKEN` pair in the environment, or the
source's `app_authentication_impact: none`); otherwise it returns `indeterminate` rather than
starting app authentication under `NONE`. See "Preview and actual resolution" in the secrets README
for the complete rule.

**What to do:** if you configured 1Password (or another provider backend) for unattended auth, know
that `agw doctor` and every command's preflight now make a real provider call on that path as a
matter of routine, which adds latency and a new failure surface (see the FAIL section above) to
operations that used to be pure and local. If that is unwanted, do not set unattended auth (or set
`app_authentication_impact: operator-action`, the default) purely to make doctor look clean; the
compact indeterminate OK note is expected in that case.

## `agw secret verify`'s output vocabulary and columns changed wholesale

Pre-0.15, `secret verify` actually resolved each secret (discarding the value) and rendered
`NAME, CATEGORY, SOURCE, IDENTIFIER, DETAIL, REMEDIATION`, with `CATEGORY` one of `resolved`,
`unavailable`, `refused-interaction`, `timeout`, `resolution-failure`. It now previews instead of
resolving, and renders `NAME, STATUS, SOURCE, IDENTIFIER, REASON, HINT` , with `STATUS` one of
`available`, `missing`, `indeterminate`, `blocked`, `failed`. The `REMEDIATION` enum is gone; `HINT`
is free text. A script that greps a column name or a `CATEGORY`/`DETAIL` value will no longer match
anything.

**What to do:** update any script parsing this table to the new headers and status vocabulary, or
switch it to the exit code alone: `agw secret verify` still exits 0 exactly when every named secret
resolves (`available`) and 1 otherwise, so a pass/fail check that never needed the table content is
unaffected. `secret verify` has no `--output json`; if you need a stable machine-readable shape,
`agw secret describe NAME --output json` does.

## `--allow-interaction` with global `--non-interactive` is no longer a hard error

Pre-0.15, `agw secret verify --allow-interaction` under global `--non-interactive` raised
`ValidationError: --allow-interaction cannot be used with --non-interactive`. That check is gone.
The two flags now compose: `--allow-interaction` grants `OperatorImpact.ALLOW` (permits provider
work that needs operator action, such as a 1Password app or biometric prompt), while global
`--non-interactive` independently disables TTY-based prompting only. Used together, a command may
still trigger out-of-band provider authentication while refusing to read the terminal.
`agw secret describe` did not previously accept `--allow-interaction` at all; it is new there too.

**What to do:** if a script relied on that combination erroring out as a guard rail, it no longer
will. Drop `--allow-interaction` to keep a run free of any operator-action provider work.

## `--non-interactive` controls TTY interaction only

Carried forward from the 0.14 guide, since this note describes current behavior rather than a 0.14-
specific transition and the 0.14 guide is scheduled for deletion: the global `--non-interactive`
flag means only "do not use the TTY for interactions, even when one is present." It does not control
color or other presentation, and it does not suppress biometric prompts, desktop app approval,
browser or device flows, or other provider work outside the terminal; that work may still wait out
the configured source timeout. The flag is not a general unattended fail-fast mode. For a truly
unattended path, use an environment-variable source, or provider authentication known to be
unattended (a supported 1Password service-account or Connect configuration), and set an appropriate
source timeout.
