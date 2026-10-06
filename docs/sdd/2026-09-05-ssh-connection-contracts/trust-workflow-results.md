# Trust Workflow Proof Evidence

This SSH-owned record tracks the composed trust proof for Phase 2. It supplies no new requirements
and does not replace the [plan](plan.md) or production creation/publication acceptance.

## Installed-client fixture composition

The developer's `9500cf4eea8646de42778d76f2d28e7547f0b791` adds four integration-marked cases. The
existing enrollment server is extracted into one ordinary owner shared by enrollment and maintenance
tests, without replacing the separate byte-delivery fixture. Its explicit server policy disables
user SSH RC execution. The cases remain Linux loopback fixtures with generated keys and fixture
creation provenance, not production provider bindings.

The composed sequence enrolls a creation candidate, publishes that candidate with complete
applicable policy through the actual maintenance CLI, and executes strictly through a managed
reference. It then blocks admission, fails a partial refresh, verifies learned-key and
prior-generation retention, repairs explicitly and reconnects. The same carrier observes fresh
policy at every step. Command markers distinguish refused effects from successful execution.
Separate cases use real hashed known-hosts records with the selected alias/nondefault port and
refuse a different lookup identity. Source bytes/timestamps, candidate metadata and partial
publication evidence are checked separately.

Project, complexity and generic private source reviews clear the exact developer pin. The developer
records **98 non-integration tests passed with 1 skip**, four native cases collected, full
Ruff/format, strict mypy (1,225 sources) and file quality passed. The lead integrates the unit at
`a5c6407bf70faeac79c63381f5d2b60b432abc8d`, independently passes **71 affected non-integration tests
with 1 skip**, collects all four new cases, and passes full Ruff/format and strict mypy. The
different focused counts reflect different selections, not native execution.

**Native cases have not run.** Collection starts no server and generates no credentials. These
results do not establish real hashed matching, actual authentication offers, native macOS/Windows
trust custody, genuine provider creation receipts, production publication/writer coexistence,
rollback or additive RunContext use. Those remain required gates. The later rebase onto transport
`833f1c280cc67f8d9b8f71f2e229ab69d20f7df5` carries this unit as `a975ce65f`; fresh combined
validation follows the next actual resize/native-primitive integration batch.

## Authentication offers and native fixture custody

The developer's `c335e81ce603c25392dca7af0a28fb2bd12d1d18` supplies six Linux integration cases.
They compare full key algorithm/blob observations from the owned server's authentication packet log.
They cover inherited-agent exclusion, explicit selection of one public identity from a multi-key
agent, matching versus stale sibling public keys and refusal to offer an automatic sibling
certificate. Positive observed offers distinguish actual authentication from acceptance alone;
unrecognized log syntax fails observation rather than becoming an absent-offer claim.

The source-pure fixture worker enters passively and admits native work through an explicit guarded
start. One retained worker owns each server/agent construction, borrowed lifetime and cleanup.
Synthetic interruption cases cover delayed startup, construction, completion waiting and cleanup
ordering. These helpers create no native resources at import or collection. Native execution
requires a tester-owned account/home or independently verified startup-free configured shell;
overriding HOME alone does not isolate OpenSSH's account-derived configuration.

All three independent source lanes clear the combined private pin
`fedaf2e8e9339d5212405f2d0ae69956fc67975e`, including these fixtures, installed POSIX terminal
composition and bounded Windows diagnostics. The lead measures **415 non-integration SSH tests
passed with 6 skips**, full Ruff/format (1,283 files), strict mypy (1,246 sources), selected Windows
typing and file quality, all exit 0. Six authentication and two POSIX terminal cases collect without
execution. A subsequent optional cleanup removes an unnecessary terminal fixture grouping marker.
Rebase onto transport `75a59e15a` at `60c1fa3843c6ad45433da92ffba83349a8b1a968` preserves the entire
SSH source/test tree after that cleanup. Fresh combined gates follow that dependency change.

The complete suite at that rebased pin passes 14,801 non-integration tests with 51 skips (exit 0).
Full Ruff/format, strict mypy (1,251 sources), typer isolation, file quality, locked-SDD, Rulesync
and website gates also exit 0. The [terminal record](terminal-results.md) preserves the exact gate
scope and the separately failed native Windows run requiring new diagnostic evidence.

**The new native cases have not run.** No default account-configuration isolation, physical keyboard
behavior, successful native offers, provider creation/publication, writer coexistence, rollback or
RunContext acceptance is inferred from their source review or synthetic gates.
