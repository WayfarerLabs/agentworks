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
