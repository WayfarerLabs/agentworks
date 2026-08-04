# FRD: Harness Integration (capability rename)

- Status: Draft
- Start date: 2026-08-03
- Related prior SDDs: `docs/sdd/2026-07-07-session-harness`, `docs/sdd/2026-08-01-codex-harness`
- Review folded in: `codex-response.md` (Codex review of the earlier draft, 2026-08-03)

## Summary

The `harness` capability is named for the wrong layer. Leading coding-agent vendors (and Anthropic's
own Claude Code glossary) use "harness" for the tool itself: "Claude Code is the harness; Claude is
the model inside it." Agentworks uses "harness" for its own capability that runs such a tool, which
collides with that meaning and has already caused confusion (the README has to put the Agentworks
"harness" in quotes to disambiguate).

**This effort is a complete, standalone rename of the capability from `harness` to
`harness-integration`, with no functional change beyond the name.** It is scoped to ship in the next
release on its own. A larger, separate follow-up (its own SDD and PR) will expand the capability
into a multi-scope tool integration; that expansion is what ultimately motivates the "integration"
name, but none of it is built here.

## Why rename, and why "integration"

Three-layer model (the reason the current name is wrong):

1. the **model** (for example, Claude): the weights.
2. the **harness** (for example, Claude Code): the tool that turns the model into an agent. This is
   the vendors' meaning.
3. the **Agentworks integration** for that harness: the capability that installs, configures,
   launches, and resumes a harness inside Agentworks. Today's capability lives here but is named
   after layer 2.

The two layers are not one-to-one, in either direction: several distinct integrations could drive
the same harness (an interactive-transcript-resume one, a headless `-p` one for unattended runs),
and a single integration could drive several harnesses (a multi-harness integration that switches
between Claude Code and Codex by workload). Either way the point is the same: Agentworks is not
running the harness, it is running the harness integration, and the name should say so.

**Name: `harness-integration`.** An earlier draft recommended `harness-adapter`, and
`codex-response.md` raised `harness-integration` as the alternative worth evaluating. "Integration"
wins because the capability's end state owns installation, authentication, configuration, workspace
publication, launch, and resume across scopes: "integration" describes that whole responsibility,
where "adapter" describes only the narrower drive-the-tool role. Since a rename is expensive and
done once, naming for the end state avoids renaming a second time when the expansion lands. `tool`
and `tool-adapter` were rejected (in agent-systems "tool" already means a model-callable action);
`workload` is reserved for the session-scoped facet, not the kind.

The naming claim is deliberately narrow and well-supported: leading vendors increasingly use
"harness" for the runtime, the term is not universally settled (it is also used for eval runners and
scaffolds; see arXiv 2606.10106, "What makes a harness a harness"), and the local collision alone
justifies the rename. Sources: the Claude Code glossary entry for "agentic harness"
(`code.claude.com/docs/en/glossary`); OpenAI's "Unlocking the Codex harness"
(`openai.com/index/unlocking-the-codex-harness/`); the survey (`arxiv.org/abs/2606.10106`).

## Scope of this effort

A complete rename. Every reference to the old name changes: the capability kind slug, the
session-template selector field, the persisted database column, all code identifiers, files and
directories, CLI-visible text, and documentation. No behavior changes; a session created before the
rename keeps working after it.

Because the name is operator-facing and persisted, the rename ships with full backward compatibility
and a migration path, on a one-release deprecation window.

## Functional requirements

- **R1 (complete rename).** The capability MUST be renamed from `harness` to `harness-integration`
  across every surface: the kind slug (and its two registration sites plus the hardcoded
  capability-kind set), the `session-template` selector field and its config pair, the persisted DB
  column, code identifiers (classes, package and module paths, the registry and its accessors,
  threaded variables), files and directories, CLI-visible strings, and docs. Historical CHANGELOG
  entries are left as an immutable record.
- **R2 (no functional change).** Behavior MUST be identical before and after, save the name. The
  three shipped implementations (`shell`, `claude-code`, `codex`) and their configs behave exactly
  as today.
- **R3 (session data migration).** Existing sessions MUST keep working. The persisted
  `sessions.harness_state` column MUST be renamed via a new forward migration with no data
  transformation and no loss; a database created before the rename MUST upgrade cleanly and its
  sessions MUST still start and restart.
- **R4 (TOML compatibility).** TOML-declared session-templates MUST continue to work as-is (they are
  already deprecated in favor of YAML resources; that status is unchanged). `agw resource migrate`
  MUST produce YAML resources that use the new selector name.
- **R5 (YAML compatibility with deprecation).** Existing YAML session-templates MUST continue to
  load, in both current shapes:
  - the tagged-table shape (`harness: { name: ..., <config> }`), and
  - the legacy flat shape (`harness: <name>` as a peer of a `harness_config:` object).

  Any use of the old `harness` selector key MUST emit a single aggregated deprecation warning
  stating that the `harness` key is deprecated in favor of `harness_integration` and will be removed
  in the next release. `agw resource migrate` MUST rewrite either old shape to the canonical
  `harness_integration: { name: ..., <config> }` form.

- **R6 (kind-slug compatibility with deprecation).** Operator references to the old kind slug
  (`agw resource list --kind harness`, `harness/<name>` addressing) MUST keep resolving for this
  release with a deprecation notice, and be removed in the next release. (Alias versus hard-cutover
  mechanics are an HLA and migration-strategy decision; the requirement is a one-release ramp.)
- **R7 (canonical output uses the new name).** Everything Agentworks emits or displays (CLI columns
  and labels, `resource sample` and `resource migrate` output, sample manifests, docs) MUST use
  `harness-integration`/`harness_integration`. The old name survives only as an
  accepted-with-warning input, never as canonical output.

## Non-goals (this effort)

- The multi-scope expansion (user-scope and workspace-scope provisioning hooks, the per-hook scope
  contract, the new lifecycle wiring). That is the deferred follow-up (see "Deferred: multi-scope
  integration"); nothing about it is designed or built here.
- Any change to `shell`/`claude-code`/`codex` behavior or config vocabulary.
- Absorbing the account-isolation strategy into the capability.
- Multiple integrations per tool as a shipped feature (the model already permits it; nothing new is
  built).

## Personas

- **Operator**: has `harness:` in session-templates, has typed `--kind harness` and
  `harness/<name>`, and has sessions already running. Needs the rename to break none of that on
  upgrade, and a clear path (a warning plus `agw resource migrate`) to the new name.
- **Maintainer / reader**: wants the capability's name to match reality (the Agentworks integration
  for a harness, not the harness), with the codebase, DB, CLI, and docs all agreeing on one name.

## Open decisions

- **D1 (deprecation mechanics).** The exact form of the compatibility layer (a kind-slug alias in
  the registry versus a hard cutover with a `doctor`/error hint; how the selector shim reuses the
  existing legacy-flat-field hoist) is settled in `migration-strategy.md`. The requirement is fixed:
  both old shapes load with an aggregated warning this release, canonical output is the new name,
  removal is next release.
- **D2 (selector spelling).** Recommendation: `harness_integration` (snake case, matching the
  internal field pair `harness_integration`/`harness_integration_config` and the other capability
  selectors `platform`/`provider`). It is a longer key than `harness:`; the length is accepted for
  consistency with the kind and vocabulary. Confirm in the HLA.

## Deferred: multi-scope integration (separate follow-up SDD and PR)

Recorded here so the name and the deferral are understood, not to be built in this effort. The
follow-up will expand the capability so a single integration also contributes provisioning at the
user and workspace scopes (not just the session), under a per-hook scope-and-privilege contract, and
will subsume today's ad-hoc, Claude-specific user-provisioning debt
(`claude_marketplaces`/`claude_plugins`) and supersede the planned `harness-user-provisioner`
capability. It carries its own requirements, interface design, and migration, in its own SDD. The
one property this rename preserves for it: the capability is named for that end state, so the
follow-up adds behavior without renaming again.
