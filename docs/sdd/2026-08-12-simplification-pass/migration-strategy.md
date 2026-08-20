# Migration Strategy: Notes Over Shims

**Status (2026-08-13)**: the breaking changes inventoried below are out of scope for this pass (FRD,
Out of scope) and are routed: they run as a dispatched task briefed on the
`refactor/breaking-truth-0-14` branch, in parallel with this pass's deletion waves. This artifact is
that task's authoritative strategy, read from this directory once this SDD is on `main`.

Operator direction (2026-08-13): the strategy for breaking changes is worth capturing as its own
artifact. It is deliberately not a cutover plan, because there is nothing to cut over: 0.14 is
unreleased, there is no deployed fleet, and no compat window to bridge. The strategy is that
migration knowledge is captured once, at commit time, and flows to the people and agents who need it
through the release pipeline and the self-documenting features, instead of living as
backward-compatibility code.

## The pipeline

One chain, no new machinery, every link already shipped:

1. **Commit**: every breaking change writes its `BREAKING CHANGE:` footer as operator-actionable
   migration guidance: what breaks, what to change, one before/after example.
2. **Changelog**: release-please accumulates those footers into `cli/CHANGELOG.md` under the release
   that ships them.
3. **Wheel**: the changelog is packaged into the distribution, so the guidance is offline and
   version-exact wherever the CLI is installed.
4. **Guide**: `agw guide concept-release-notes/vX-Y-Z` renders the packaged section as bounded,
   untrusted-evidence text, and `agw guide concept-migration` teaches consuming it across an
   installed-version span.
5. **Assistant agents**: the always-available assistance flow reads the guide, so an agent helping
   an operator across a break works from the packaged notes for the exact versions involved, without
   any in-code compat path to discover or maintain.

The default answer to a break is therefore a note, not a shim. Compat code is the exception, and per
this SDD's acceptance it requires an explicit operator decision with a recorded expiry.

## Inventory: the candidate breaks (saga routing)

| Change                     | Current                                                                                     | Target                                                   |
| -------------------------- | ------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| Secret mapping key (S5)    | `[secret_config].backends`, an ordered list of source names: the key misnames what it holds | the key says `sources`; the config name matches the fact |
| Token config (C3)          | five spellings via a one-arm tagged union                                                   | the concrete stored-token shape, one spelling per fact   |
| Env entry compat flag (C4) | `canonicalize_null_companions` re-accepts and advertises a retired spelling                 | retired spelling rejected; schema stops advertising it   |
| Compat layers (C7)         | four layers plus two uninventoried objects, no expiry                                       | deleted now, or quarantined with a recorded expiry       |

Each change's PR carries its footer per the pipeline above and updates
`docs/guides/upgrading-to-0.14.md` in the same PR, which remains the single consolidated
operator-facing walkthrough for the release.

## Worked example (S5 rename)

**Historical.** The footer shape below predates the parser-safe convention and is exactly the shape
Release Please truncated in the generated 0.14.0 release PR; both affected entries shipped repaired
in 0.14.0's `cli/CHANGELOG.md`. `CONTRIBUTING.md`'s Conventional Commits section now states the
current, parser-safe convention (one paragraph, no blank lines or indented code blocks, before/after
examples inline); follow that, not the shape below.

Footer shape the convention requires:

```text
BREAKING CHANGE: [secret_config].backends is renamed to sources; the
ordered precedence list always held source names. Rename the key in
config.toml. Before:

    [secret_config]
    backends = ["env-var", "prompt"]

After:

    [secret_config]
    sources = ["env-var", "prompt"]
```

The rename is the configuration key only. The inspection JSON's `source` and `backend` fields name
two different facts (the configured source instance and its implementing backend capability) and
both stay, per the locked secret-sources design.

What an operator or agent sees after upgrading: `agw guide concept-release-notes/v0-14-0` renders
that text verbatim as packaged evidence; the assistance flow relays the rename and the one-line
edit. Nothing in the CLI parses the old key; the error for an unknown `backends` key points at the
upgrade guide.

## Sequencing and safeguards

- The convention (standard `BREAKING CHANGE:` footers written as operator-actionable guidance, plus
  the existing upgrade guide) needs no custom enforcement machinery; the footer shape above and
  ordinary review are the mechanism.
- If a release window closes before a candidate lands, the item goes back to the operator for
  replanning; no compat layer appears as an automatic fallback.
- The bootstrap prompt's stop-path already prevents the worst mismatch: an assistant agent with no
  compatible stable release available stops rather than improvising against the wrong version's
  contracts.
