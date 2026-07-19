# Plan: named-console-template instance selector

Implements the [FRD](./frd.md) per the [HLA](./hla.md). Mirrors the shipped admin-template selector
(PR #200) except for the `NamedConsoleConfig` plurification prework.

Each phase ends with the full gate green: from `cli/`,
`uv run pytest -q && uv run ruff check . && uv run mypy agentworks`; then from repo root
`./scripts/lint-files.sh`. Definitions of done are per-phase below. Every new behavior lands with
tests in the same phase.

## Phase 0: coordination and baseline

- [ ] Confirm the admin half (PR #200) has merged to `main`; rebase this branch onto it. If it has
      not merged, hold implementation of the DB and envelope phases (they conflict) and proceed only
      with design.
- [ ] Recompute the migration number: `LATEST_VERSION` after #200 is 29, so this console migration
      is **30**. Record the final number here once confirmed: **\_\_**.
- [ ] Confirm `_NO_SELECTOR_KINDS` on the rebased base is `{"named-console-template"}` (admin
      already removed).

**Done when:** branch is rebased on merged #200, the migration number is fixed, and the gate is
green on the rebased base with no changes yet.

## Phase 1: plurify `NamedConsoleConfig` and its kind

- [ ] Read the resource protocol to settle the HLA open item: does `NamedConsoleConfig` need a
      `referenced_resources` method? Record the answer here: **\_\_**.
- [ ] Add `name: str = "default"` to `NamedConsoleConfig` (`sessions/template.py`); add a trivial
      `referenced_resources` returning `[]` only if Phase 1's first item says it is required.
- [ ] `_NamedConsoleTemplateKind.synthesize` builds
      `NamedConsoleConfig(name="default", origin=...)`.
- [ ] `_NamedConsoleTemplateKind.instances` filters consoles by matching
      `(console.template or "default")` against `resource.name` (replaces the yield-all-consoles
      singleton behavior). Depends on Phase 2's column; if Phase 2 lands first, wire the filter here
      against it.
- [ ] `named_console_template(registry, name: str = "default")` in `resources/access.py` resolves by
      name (backward compatible default).
- [ ] Tests: `NamedConsoleConfig` carries `name`; `synthesize(())` yields `name="default"`; the
      registry holds multiple named `named-console-template` rows and looks them up independently
      (mirror `test_admin_template_plurified.py`'s framework-shape tests).

**Done when:** the framework treats `named-console-template` as named-multi-instance with the same
shape as the other template kinds, with tests pinning it.

## Phase 2: DB column and migration

- [ ] Migration **30** (the Phase 0 number): `ALTER TABLE consoles ADD COLUMN template TEXT`
      (nullable, `NULL` = default). Forward-only, no backfill.
- [ ] `ConsoleRow.template: str | None`; `_to_console` reads it; `insert_console` gains
      `template: str | None = None` and writes it.
- [ ] Tests: a console inserted with `template="wide"` round-trips; `NULL` reads back as `None`; the
      migration test's "advances to `LATEST_VERSION`" assertion still holds (update if it pins a
      literal old max).

**Done when:** consoles persist a nullable template selection and the column round-trips.

## Phase 3: `console create` selector and manager error path

- [ ] `console create` (`cli/commands/console.py`) gains `--template <name>`, threaded into the
      console manager create call. CLI body stays thin.
- [ ] The console manager resolves the selected name; an unknown non-`default` name raises the
      uniform `unknown_template_error` (kind `named-console-template`, with the declared names as
      the hint) before any DB write or SSH work. Persist canonical `NULL` for the reserved default
      (omitted or explicit `default`).
- [ ] Tests: create with a declared template persists it; create with no flag persists `NULL`;
      create with an unknown name errors naming it, with no console row inserted and no on-VM work
      (assert the typed error and zero rows).

**Done when:** operators select a template at create time and unknown names fail early and cleanly.

## Phase 4: layout consumers read the console's template

- [ ] Audit the four `named_console_template(registry).tmux_layout` sites in
      `sessions/multi_console.py` (around lines 445, 711, 764, 2030); confirm each has the
      `ConsoleRow` in scope and record any that do not.
- [ ] Thread `named_console_template(registry, console.template or "default").tmux_layout` at each
      in-scope site.
- [ ] Tests: a console on a non-default template lays out per that template's `tmux_layout` at the
      layout-application paths (attach / restart / add-shell), driven by the persisted column.

**Done when:** every layout-application path honors the console's own template.

## Phase 5: manifest decode, envelope, completions

- [ ] `_decode_named_console_template` threads `name=doc.name`; `_load_named_console` gains
      `name: str = "default"` (TOML path stays singleton, R7).
- [ ] Remove `"named-console-template"` from `_NO_SELECTOR_KINDS` in `manifests/envelope.py`. The
      set is now empty: delete the set and its special-casing branch, and update the envelope test
      that pinned the rejection (the admin half re-pointed it at `named-console-template`; with no
      no-selector kinds left, replace it with a test that a named `named-console-template` manifest
      is accepted, and drop the now-unreachable rejection test or convert it to assert acceptance
      for both kinds).
- [ ] Add the `console_templates` dynamic completer: `completions/spec.py` (identifier +
      `("console.create", "template")` + doc comment) and the three shell backends, mirroring
      `admin_templates`.
- [ ] Tests: a named `named-console-template` manifest loads and is selectable (previously
      rejected); a `[named_console.wide]` TOML block does not create a second instance (R7);
      completions test covers `console_templates`.

**Done when:** named instances declare normally via manifest, the envelope no longer special-cases
any kind, #165's `_NO_SELECTOR_KINDS` is gone, and TOML stays singleton.

## Phase 6: docs, close-out, lock

- [ ] `cli/README.md`: document `--template` on `console create`; note the kind is now selectable in
      the resource inventory if it is listed there.
- [ ] Full regression pass green; confirm issue #165's acceptance criteria (both kinds selectable,
      envelope special-casing gone).
- [ ] agentworks-reviewer round; address findings.
- [ ] Non-draft PR; iterate on Copilot (subject to quota).
- [ ] On merge: write `locked.md` summarizing what shipped, the permanent homes (the selector story
      lives in `cli/README.md` and the code; nothing load-bearing stays under `docs/sdd/`), and note
      that issue #165 is fully closed. Carry any deferrals into the lockfile.

**Done when:** the console selector ships, docs reflect HEAD reality, and the SDD is locked.

## Permanent homes (SDD-not-permanent)

- **`cli/README.md`**: the `console create --template` surface. This is the operator-facing home.
- **The code**: `_NamedConsoleTemplateKind`, the `consoles.template` column, and the name-aware
  accessor are self-documenting via their own docstrings, matching the admin-template equivalents.
- Nothing under this SDD directory is load-bearing after merge; the directory is deletable.

## Notes carried from design

- The issue's `SYNTHESIZED_SINGLETON_KINDS` reference is stale (the constant is gone); R7 is
  enforced by `_load_named_console` staying singleton, not by a constant.
- No secret-projection or orchestration changes: `named-console-template` carries only
  `tmux_layout`, with no `env`, so there is no `_secrets_reachable_from_session` analogue (unlike
  the admin half).
