# Plan: The agentworks.build Website

- Status: Phase 4S free-flight and terrain-variety correction in design; canonical assistance
  integrated
- Date: 2026-08-07
- Last revised: 2026-08-14
- FRD: `frd.md`
- HLA: `hla.md`
- Research: `prior-art-research.md`

## Delivery shape

This effort records the initial website release, the canonical assistance integration, the
continuous Lander refinement, and a final closeout. Phases 0 through 6 preserve the first-release
journey: PR #439 delivered the useful Home, Manifesto, Security, Lander, custom 404, shared
shell/game contracts, CI, Pages deployment, and runbook without pretending the still- developing
assistance prompt existed.

The operator's 2026-08-10 correction then authorized the onboarding-and-discovery effort lead to
integrate the canonical assistance source and reconcile this effort's FRD, HLA, plan, and shell LLD
inside PR #480. Phases 7 and 8 are that jointly delivered integration, not a later main-only PR: the
same feature PR replaces the retired availability notice with the canonical prompt and copy
enhancement without redesigning the site or creating another deployment path. The website effort
retains ownership of production acceptance and the final closeout PR.

PR #486 carries the continuous Lander work. Phases 4A through 4O are accepted gameplay history;
Phase 4P's automated record remains immutable but its product behavior was rejected by the operator,
Phase 4Q restores the accepted fixed-scene baseline, Phase 4R owns the straight-terrain correction,
and Phase 4S owns the final free-flight and terrain-variety follow-up. The PR preserves the
established site, canonical assistance projection, hosting, and routes throughout that recovery.

`locked.md` never merges before R1-R25 and R28-R29 plus AC1-AC26 and AC29-AC30 are accepted in
production; superseded R26-R27 and AC27-AC28 are historical evidence, not closeout gates. Every
earlier merge is independently useful and operable. The first release was honest without an
onboarding claim, and the integrated release consumes the canonical source from the same reviewed PR
while preserving deterministic source identity; no permanent website code binds to a branch-only or
duplicated contract.

## Phase 0: design convergence and coordination

- [x] FRD ownership/status, prior-art research, HLA, and this plan are internally consistent and
      pass repository file-quality checks.
- [x] Draft design PR opened for pre-implementation review and explicitly sent to the roadmap lead,
      because the roadmap lead seeded this standalone effort and reviews its PRs.
- [x] A pre-PR internal `agentworks-reviewer` pass reviewed the initial artifacts against the FRD,
      project principles, SDD process, and roadmap constraints; its valid findings were resolved
      before the draft PR opened.
- [x] Hosting choice, one-page scope, no-analytics posture, canonical apex, and external GoDaddy
      setup are accepted by the operator or revised in the owned artifacts.
- [x] Operator selects the symmetric custom AGW mark with the original twin layered plumes and the
      hidden lunar-deployment 404 direction.
- [x] Roadmap-lead findings are resolved: content-anchored passage selection, deploy-trigger
      completeness, review-box wording, content-class count, named build inputs, README fence
      pickup, and dated operator evidence.
- [x] The revised FRD, HLA, plan, prior-art research, brand direction, and SVG concepts receive a
      fresh `agentworks-reviewer` pass; all valid findings are resolved and re-reviewed.
- [x] Operator directs PR #439 to remain an unmerged draft until the complete site is ready; local
      demo implementation branches from its review-clean design head.
- [x] A fresh `agentworks-reviewer` reviews the staged-release, security, onboarding-disclosure,
      terminal/TUI, and interim-acceptance revisions; all valid findings are resolved and
      re-reviewed.
- [x] Saga lead reviews and blesses the staged interim release, dedicated security page, and
      terminal/TUI visual direction before Phase 3 implementation begins.
- [x] After that blessing, deliver a single-file coordination message through `main` to the
      onboarding effort: its canonical disclosure must say the agent runs on the intended
      workstation and needs full file inspection and command execution access as the workstation
      account, without implicit root. Elevation stays separate and explicit; a strict posture
      governs approval and visibility without blocking required access. Do not modify that effort's
      owned artifacts from this branch.

Operator evidence:

- 2026-08-07: GitHub Pages, one-page scope, no analytics, canonical apex, operator-approved GoDaddy
  cutover, and WCAG 2.2 AA acceptance remain first-slice rulings. Scriptable evidence is preferred;
  the screen-reader pass remains manual.
- 2026-08-08: the operator selects the custom symmetric AGW rocket with the original twin plumes, a
  hint-free preflight 404, Space/arrow/vi keyboard play, tap/hold/drag mobile play, and successful
  agent deployment into a visibly powered NOC followed by lander departure as first-slice
  requirements.
- 2026-08-08: the operator rejects a separate design-artifact merge. PR #439 stays draft until the
  complete site is ready, while the game becomes available as a local demo without public
  deployment.
- 2026-08-09: the operator proposes making the complete non-onboarding site public while onboarding
  remains in development. Everything stable, including the site shell, repository-derived content,
  brand, custom 404, CI, hosting, and domain, may ship first; the site must omit rather than invent
  onboarding text.
- 2026-08-09: the saga lead blesses the staged interim release, dedicated security page, and
  terminal/TUI visual direction. Phase 3 implementation is authorized.
- 2026-08-09: merged coordination PR #464 delivers the required canonical workstation-access
  disclosure to the onboarding effort as a single new message file. The onboarding owner now has the
  requirement on `main`; its implementation remains a prerequisite only for the later bootstrap
  integration, not the independent interim shell.
- 2026-08-09: isolated Chromium 151 acceptance at interim-shell head `1798ca9a` passed 113 of 113
  measured assertions across the shared shell, game, responsive layouts, reduced motion,
  accessibility-tree contracts, lifecycle, requests, storage, and clean-context comprehension with
  no product defect.
- 2026-08-09: the operator narrows the pre-merge browser gate to Chrome and Edge, the two browsers
  available on the machine that can reach the forwarded preview. Firefox/WebKit, spoken
  screen-reader quality, physical mobile/touch hardware, and broader device coverage move to
  post-launch production validation; defects found there are follow-up work. Those rows remain
  explicit and must close before the final production-acceptance record and `locked.md`.
- 2026-08-10: the operator confirms the forwarded preview is fine in current Chrome and Edge and
  authorizes merge after two final presentation changes: make TOC entries slightly smaller than body
  text and bold the complete `SSH-over-Tailscale control plane` phrase on Home. Exact browser build
  strings were not supplied; the permanent checklist records that limitation and keeps the broader
  post-launch engine/device rows open.
- 2026-08-10: the operator explicitly approves the continuous expedition as a new stacked PR after
  reviewing its design: a continuous seeded world, varied lunar terrain, one battery-and-antenna NOC
  beside each elevated three-lander-width pad, carried fuel and consumable cans, offscreen-next
  cues, stronger physics, vacuum crash presentation, last-pad restart, and a decaying multiplier on
  a demonstrated route allowance. This deliberately expands the optional first-release surprise to
  R21-R23 without making PR #439's reviewed site merge-readiness depend on the redesign. The initial
  constructive pass evaluated 45 finite candidates for each of nine templates; the final Phase 4H
  recipe evaluates 81 per template, 729 total, and runtime performs only the selected template's two
  proof replays. Neither is the earlier up-to-two-million runtime-search concept questioned at the
  design checkpoint.
- 2026-08-09: with operator approval, repository Pages is enabled with GitHub Actions as its source
  at the default project URL and HTTPS enforcement on. The automatically created `github-pages`
  environment uses custom deployment-branch policies and has exactly one policy, branch `main`; no
  deployment occurs before the publishing workflow runs from a merged `main` commit.
- 2026-08-09: as a narrow expansion from the original one-page scope, the operator adds a visually
  secondary `We take security seriously.` path to a dedicated security deep dive; the home page
  stays concise and non-preachy. The operator also requires the eventual onboarding disclosure to
  identify the intended workstation, the agent's necessary full local file/command access, and the
  recommendation for a strict, observable harness posture. Across the site, terminal/TUI cues should
  communicate `simple but powerful` without turning the site into a fake terminal.
- 2026-08-09: after reviewing the local interim shell, the operator directs a tighter landing page:
  remove the rendered manifesto/problem-and-principles sections, expose each repository, package,
  rationale, and security destination exactly once, and enlarge the selected rocket by roughly two
  to three times. The dedicated security deep dive and host-required 404 remain separate optional
  surfaces; "single page" governs the primary product experience rather than eliminating those
  previously accepted routes.
- 2026-08-09: after reviewing the compact shell, the operator assigns each destination one
  conventional location: GitHub and PyPI at the top right with icons, rationale and security at the
  footer right, and Wayfarer Labs ownership at the footer left. Home and 404 gain the same
  breadcrumb-led header; the 404's linked Agentworks crumb becomes its route-home action, retains a
  small adjacent rocket, and Home omits that small mark because its large hero follows.
- 2026-08-09: the operator clarifies that every non-Home page, including Security, keeps the small
  breadcrumb-adjacent rocket. The repository's long-form Why document is becoming the Agentworks
  Manifesto and should render as a first-class generated `/manifesto/` page, using the same
  canonical-source model as Security rather than linking visitors out to the repository.
- 2026-08-09: after inspecting the extraction mappings, the operator simplifies long-form content
  ownership. Manifesto renders the complete `docs/why-agentworks.md` today and Security renders the
  complete root `SECURITY.md`; neither page selects or duplicates passages in website code. The
  future Why-to-Manifesto rename changes the one Manifesto source path to `docs/manifesto.md`, with
  no fallback or simultaneous support for both names.
- 2026-08-10: the reviewed repository-document rename lands with `docs/manifesto.md` as the one
  canonical Manifesto source. Website code, tests, and this architecture contract move in lockstep;
  the former source path is removed rather than retained as compatibility machinery.

Definition of done: requirements and architecture are review-clean, operator-significant choices are
settled, and PR #439 truthfully carries the reviewed contract that implementation follows.

## Phase 1: brand and lunar-deployment LLD

- [x] Delegate `brand-and-lander-lld.md` to an `agentworks-dev` subagent. It pins final asset names,
      reusable SVG groups, responsive scene geometry, state machine, physics constants, landing
      envelope, deterministic stepping, plume scaling, keyboard and pointer mappings, lifecycle
      cleanup, reduced-motion behavior, NOC/landing geometry, deployment and departure sequence,
      accessible status, and test vectors.
- [x] Lead reviews the LLD against R6-R9 and D5/D7, resolving any conflict between the hidden
      reveal, useful static 404, mobile controls, and WCAG 2.2 AA.
- [x] `agentworks-reviewer` reviews the LLD; all valid findings are resolved and re-reviewed.

Definition of done: the selected mark and bounded game are specified at implementable detail without
introducing a framework, remote asset, hidden critical content, or onboarding dependency.

## Phase 2: selected assets and custom 404

- [x] Delegate implementation to an `agentworks-dev` subagent with ownership of the selected
      permanent SVG assets, 404 HTML/CSS/JavaScript, the narrow 404-only builder seam, focused game
      tests, and package-free manual browser checklist. The subagent is not alone in the codebase
      and must preserve concurrent changes.
- [x] Promote the selected original twin-plume geometry into a self-contained permanent SVG under
      `website/`; do not make permanent code depend on numbered SDD concepts.
- [x] Build a useful semantic no-JavaScript 404 with a visible path home and no preflight visual
      control hints. The standard-library builder must render it with a validated root site base for
      the local operator demo; no deployment workflow or public host is added in this phase.
- [x] Implement the under-five-second plume cue, reduced-motion static state, deliberate Space or
      activation start, arrow/vi keyboard controls, tap/hold/drag pointer controls, and scoped event
      suppression and cleanup.
- [x] Implement deterministic bounded-step flight, collective and differential plume response,
      landing/crash/restart/exit states, G-bay agent deployment, persistent-per-run NOC power-up,
      lander departure, and the exact success status.
- [x] Add deterministic unit/state tests, source-contract tests, and a package-free manual browser
      checklist for input equivalence, physics vectors, landing thresholds, hidden-until-start
      controls, lifecycle pause, reduced motion, pointer capture cleanup, powered-NOC reset
      boundaries, no-JavaScript fallback, and responsive scene bounds.
- [x] `agentworks-reviewer` and a fresh-eyes reviewer inspect the slice; valid findings return to
      the implementing subagent and are resolved before re-review.
- [ ] Complete the checklist's pending Firefox, WebKit, screen-reader, human touch/motion, and
      broader-device rows as post-launch production acceptance before the final closeout and
      `locked.md`.

Definition of done: the brand and 404 game are review-clean and locally demonstrable from PR #439,
but are not deployed or merged as a partial website.

## Phase 3: interim shell LLD

- [x] Check the website feature directory on `origin/main` for late messages before starting.
- [x] Delegate `site-shell-lld.md` to an `agentworks-dev` subagent. It pins exact current-`main`
      product/security source paths and selectors, extraction errors, the exact site-owned
      availability notice and security-link label, site files and URLs, closed template vocabulary,
      page outlines, responsive layout, terminal/TUI-derived visual tokens, builder CLI, output
      tree, Pages-base transition, and interim test matrix.
- [x] Pin the future bootstrap insertion point as layout structure only. Do not inspect or depend on
      the onboarding feature branch, invent onboarding text, add a copy control, or create a runtime
      release-mode abstraction.
- [x] Lead reviews the LLD against R10/R11/R13, AC11/AC13/AC14, C5, and D1/D3/D5/D10 and updates the
      lead-owned artifacts if detail exposes a gap.
- [x] `agentworks-reviewer` reviews the LLD; all valid findings are resolved and re-reviewed.

Definition of done: the useful non-onboarding site is pinned at implementable detail against merged
sources, with an explicit removable notice and no branch-only or speculative onboarding dependency.

## Phase 4: interim site implementation

- [x] Delegate implementation to an `agentworks-dev` subagent with ownership of `website/`, focused
      website tests, and generated-output ignore entries. The subagent is not alone in the codebase
      and must preserve concurrent changes.
- [x] Extend the Phase 2 builder into the deterministic home/security/404 build per the shell LLD;
      write only below an explicit output directory, leave output uncommitted, and keep one
      rendering path for local, default-Pages, and custom-domain bases.
- [x] Generate every product/security passage from uniquely selected permanent-doc sections using
      the closed normalization contract; fail on missing/duplicate headings, content drift,
      reporting-link drift, or unsupported Markdown. Keep site-owned connective copy free of product
      and security claims.
- [x] Implement semantic document structure, metadata, stable GitHub/PyPI/rationale links, the
      secondary `We take security seriously.` link, the sourced security deep-dive page and private
      reporting path, local responsive CSS, the selected AGW visual system, and the ordinary-text
      onboarding availability notice.
- [x] Express `simple but powerful` through restrained terminal/TUI cues shared by home, security,
      and 404: monospaced accents, bounded regions, compact status details, strong hierarchy, and
      efficient density. Reject fake terminal chrome, green-screen pastiche, decorative command
      noise, or interactions that assume keyboard/terminal fluency.
- [x] Assert that the artifact has no bootstrap code region, install command, copy control/script,
      empty onboarding container, alternate release mode, or unexpanded template token.
- [x] Add deterministic, malformed-input, escaping, no-external-runtime, product/security
      source-extraction, reporting-link, DOM-contract, responsive, accessibility, terminal-cue, and
      no-JavaScript tests.
- [x] Add `website/README.md` with local build/test instructions, content ownership, output
      contract, staged release, Pages/DNS setup, deployment, rollback, and hosting-migration
      guidance.
- [x] Update the top-level `README.md` component inventory in the same PR so it no longer claims
      `cli/` is the repository's only component after `website/` lands.
- [ ] Complete the Phase 2 Firefox, WebKit, screen-reader, human touch/motion, and broader-device
      checks after launch on available public-host surfaces, before the final closeout and
      `locked.md`.
- [x] Run focused tests and `./scripts/lint-files.sh --fix`; confirm generated output does not
      appear in Git status.
- [x] `agentworks-reviewer` and a fresh-eyes reviewer inspect the implementation; valid findings go
      back to the implementing subagent and are resolved before re-review.

Definition of done: R4-R11, R13, C1-C5, and the local portions of AC5-AC11/AC13-AC14 hold in a
review-clean, deterministic interim artifact that is ready to publish but makes no onboarding claim.

## Phase 4A: operator landing-page refinement

- [x] Revise `site-shell-lld.md` to pin the compact landing outline, unique-destination link
      contract, dominant hero-logo treatment, and unchanged optional security/404 routes.
- [x] Remove rendered problem/principles content from the landing page while preserving the
      permanent source as the single deeper-rationale destination and keeping concise product
      identity sourced from the repository.
- [x] Present the selected AGW rocket as the landing hero at roughly two to three times its original
      header scale; retain reflow, focus, contrast, and 400-percent-zoom behavior.
- [x] Ensure GitHub, PyPI, rationale, and security each have exactly one landing-page anchor, with
      no repeated header/footer destinations or alternate labels that imply different content.
- [x] Update permanent website documentation and focused tests for the revised page/content
      contract; rebuild the served preview for operator review.
- [x] Run focused gates, then obtain `agentworks-reviewer` and fresh-eyes approval; resolve every
      valid finding before considering the refinement complete.

Definition of done: R14 and AC15 hold without weakening the previously accepted onboarding,
security, 404, source-ownership, responsive, accessibility, or deterministic-build contracts.

## Phase 4B: shared navigation and generated Manifesto

- [x] Revise `site-shell-lld.md` and the permanent website runbook to pin one responsive
      breadcrumb/header/footer shape across Home, Manifesto, Security, and 404, including exact link
      placement, labels, icon semantics, logo exceptions, and the 404 route-home replacement.
- [x] Move GitHub and PyPI to the header as single icon-and-text calls to action; move the sole
      Agentworks Manifesto and security links to the footer beside the exact Wayfarer Labs ownership
      text.
- [x] Implement linked `Agentworks` plus non-linked current-page breadcrumb semantics for Home,
      Manifesto, Security, and 404; omit the small mark only on Home and remove the separate 404
      body return-home link.
- [x] Generate `/manifesto/` from the complete reviewed long-form argument in
      `docs/why-agentworks.md`, explicitly map its allowed source-relative links, and add no second
      hand-maintained product or principle prose.
- [x] Retire the obsolete `--only 404`/`build_404` partial artifact and run all game builds and
      demos through `/404.html` in the complete linked artifact; remove every
      missing-local-reference exception and update permanent preview guidance.
- [x] Extend fail-closed template and generated-document tests to bind each destination, label,
      location, icon, breadcrumb state, logo exception, footer ownership string, and no-duplicate
      invariant at both supported site bases.
- [x] Adversarially prove the shared-shell validator rejects missing or reordered landmark classes,
      HTML-hidden calls to action, reviewed CSS declarations outside the closed vocabulary, extra or
      misplaced icons and rockets, and duplicate normalized local destinations introduced anywhere
      in a page. Real-browser acceptance owns computed visibility, bounds, and pointer reachability
      rather than an exhaustive CSS-concealment parser.
- [x] Rebuild the served preview and run focused website/game, lint, locked-SDD, Rulesync, and diff
      gates without regressing no-JavaScript recovery, accessibility, reflow, or deterministic
      output.
- [x] Obtain `agentworks-reviewer` and fresh-eyes approval and resolve every valid finding before
      considering the refinement complete.

Review evidence: project reviewer and fresh-eyes passes approved the Phase 4B implementation after
their findings were resolved, and the saga lead independently re-executed exact head `29ee7283` in
PR #439's dated delta review.

Definition of done: R15-R17 and AC16-AC18 hold, the revised navigation is conventional and
predictable, the Manifesto is canonical-source generated, and the site's tiny information
architecture has no duplicate destination or hidden menu.

## Phase 4C: dedicated Lander route and footer easter egg

- [x] Amend the FRD, HLA, `site-shell-lld.md`, and `brand-and-lander-lld.md` to pin the dedicated
      `/lander/` surface, shared game fragment, site-wide footer rocket link, and compact 404 title.
- [x] Incorporate and review PR-feedback rulings for root-base redeployment, native lifecycle
      controls, module sizing, independent manifest/import and URL-escaping witnesses, honest CSS
      coverage, current topology and overlap, hero baseline, and the widened ready gate.
- [x] Before further builder growth, split content projection and site validation into focused
      sibling modules and split the mirrored tests so every production and test module remains below
      1,000 lines while `website/build.py` remains the sole CLI.
- [x] Add a semantic `/lander/` shell and render one validated `#lander-game` template fragment into
      both it and `404.html`; keep one controller/model/CSS implementation and preserve all game
      mechanics, IDs, no-JavaScript behavior, and lifecycle boundaries.
- [x] Add native active-state `Exit mission` and terminal-state `Restart mission` buttons to the
      shared fragment; keep them hidden during preflight and route clicks through the existing
      controller/model events and focus lifecycle.
- [x] Add the final icon-only AGW rocket link to every footer, targeting `/lander/#lander-game`;
      give it an independent accessible name, retain one Lander destination per page, and keep it at
      the lower right in wrapping document flow, with a visible-focus target of at least 24 by 24
      CSS pixels without enlarging the mark.
- [x] Remove the redundant 404 error-code eyebrow and apply the same compact detail-page
      header-to-title inset used by Manifesto and Security while retaining the useful explanatory
      copy below `Page not found`.
- [x] Extend the exact template, image, route, fragment, manifest, metadata, CSP, heading,
      local-link, deterministic-build, and no-JavaScript contracts for five pages and ten emitted
      files.
- [x] Replace production-derived manifest expectations with a literal test-owned ten-file contract,
      prove every local JavaScript import resolves into it, add a malicious URL-attribute escaping
      witness, and narrow CSS automation claims while adding computed browser visibility coverage.
- [x] Update permanent website build/demo documentation to use `/lander/` while continuing to test
      the actual `/404.html` fallback; run the complete focused and repository gates.
- [x] Obtain `agentworks-reviewer` and fresh-eyes approval for the implementation and resolve every
      valid finding before considering the refinement complete.

Review evidence: after two Important findings were fixed, the project reviewer approved exact head
`6438b2b5`; an independent fresh-eyes pass then approved the same head with no findings. Both passes
re-executed the focused Python and Node suites and repository quality gates.

Definition of done: R7-R9, R15-R18, and AC5-AC8/AC13/AC16/AC18-AC19 hold. `/lander/` and `/404.html`
render the exact same reviewed game subtree, the footer rocket is the only deliberate game
destination, touch and assistive users can exit and restart, manifest/import and attribute
boundaries fail closed without circular tests, production/test modules remain below 1,000 lines, and
the 404 begins directly with its title after a compact natural gap.

## Phase 4D: complete Markdown pages

- [x] Amend and review the FRD, HLA, this plan, and shell LLD so Manifesto and Security each render
      one complete Markdown source, including its sole `h1`, while preserving the safe closed
      renderer and shared site shell.
- [x] Expand root `SECURITY.md` into the complete standalone security deep dive, retaining
      GitHub-only private vulnerability reporting and candid threat, boundary, operator, credential,
      limitation, scope, and upstream guidance.
- [x] Replace Security's selected-passage template and Manifesto's hash/heading inventory with one
      complete-document projection per page. Keep `docs/why-agentworks.md` as the only current
      Manifesto path; do not add a `docs/manifesto.md` fallback.
- [x] Remove duplicated security prose, source hashes, heading inventories, expected passage
      sequences, and long-form heading-path selectors from website code and tests. Retain Home's
      bounded README identity projection and explicit safe-link review.
- [x] Prove supported source additions and edits flow into generated HTML without website-code
      changes, while missing/unreadable input, invalid UTF-8, malformed structure, unsupported
      Markdown, unsafe links, GitHub-only reporting violations, and token-placement drift fail
      closed.
- [x] Update permanent website documentation and CSS for whole-document ownership; preserve the
      exact ten-file deterministic artifact, no-JavaScript content, shared navigation, metadata,
      accessibility, and all Lander/404 behavior.
- [x] Run focused and complete repository gates, rebuild the port-8766 preview, and obtain both an
      `agentworks-reviewer` and fresh-eyes approval before updating the ready PR.

Review evidence: the project reviewer and independent fresh-eyes reviewer approved exact head
`6f3dbcc4` after complete-document coupling, unsupported Markdown, and duplicate-ID findings were
fixed. Focused suites passed 103 Python and 22 Node tests; the full CLI suite passed 6,565 tests;
exact root and project-base builds remained deterministic ten-file artifacts.

Definition of done: `/manifesto/` is the complete safe HTML rendition of `docs/manifesto.md`,
`/security/` is the complete safe HTML rendition of root `SECURITY.md`, the site contains no second
long-form prose model, and the reviewed source rename changes exactly one configured path without
fallback or autodetection.

## Phase 4E: source-derived long-form navigation

- [x] Amend the FRD, HLA, shell LLD, plan, and permanent website documentation with the automatic
      `h2`/`h3` contents-navigation contract.
- [x] Generate one labeled contents navigation from the same validated document blocks, place it
      immediately after the source `h1`, preserve source order, nest `h3` entries under their
      preceding `h2`, and resolve every link to the existing generated heading ID.
- [x] Keep the default layout inline and add a wide-screen two-column article layout that places the
      same navigation on the left without changing DOM order, adding JavaScript, or weakening narrow
      viewport and zoom behavior.
- [x] Add independent source-to-navigation semantic witnesses, synthetic `h2`/`h3` nesting and
      escaping coverage, fragment validation, no-heading behavior, and CSS layout assertions without
      pinning the current documents' heading inventories.
- [x] Run focused and complete repository gates, rebuild the port-8766 preview, obtain an
      `agentworks-reviewer` approval, update PR #439, and monitor its required checks.

Review evidence (2026-08-10): the first `agentworks-reviewer` pass found that the wide grid held the
document body below the complete contents rail and that declaration-only CSS assertions could not
prove geometry. The implementation grouped the post-`h1` body beside the rail and added computed
Chromium geometry witnesses at 1600 and 390 CSS pixels. A follow-up required the browser witness to
fail instead of skip when no supported browser exists. GitHub Actions then exposed an
iframe-specific Chromium hang; the final test-only fix measures a same-origin copy of the complete
generated document directly. The reviewer approved exact commit `264bfb92` with no Blocking or
Important findings. The exact commit passed 108 Python website tests, 22 Node model tests,
deterministic root and project-base ten-file builds, all complete repository CI jobs on Python
3.12/3.13/3.14, Ruff and mypy, file lint, locked-SDD and Rulesync drift checks, and CodeQL. Its
root-base artifact was rebuilt and all five routes returned HTTP 200 from the port-8766 preview.

Definition of done: Manifesto and Security automatically expose an accessible `h2`/`h3` contents
navigation derived from their complete current Markdown source, inline after `h1` by default and in
a left rail only when enough horizontal room exists, with no manually synchronized heading
inventory.

## Phase 4F: flame-free site favicon

- [x] Amend the FRD, HLA, shell LLD, plan, and permanent website runbook with the favicon contract
      and expanded eleven-file artifact.
- [x] Add one transparent SVG favicon containing only the neutral A/G/W mark, reference it exactly
      once from every generated page, and preserve root and project-base rendering.
- [x] Add independent manifest, template-mutation, local-reference, flame-absence, self-containment,
      and exact selected-mark geometry witnesses.
- [x] Run focused and complete gates, obtain `agentworks-reviewer` approval, rebuild the port-8766
      preview, update PR #439, and monitor every required check.

Review evidence (2026-08-10): the `agentworks-reviewer` required token-aware canonical/favicon
relationships, exact closed SVG structure on both the favicon and canonical rocket, and the
canonical mark's direct-child hierarchy. After mutation witnesses closed each gap, the reviewer
approved exact implementation commit `2dbc9b8e` with no findings. Focused suites passed 114 Python
website tests and 22 Node model tests; deterministic root and project-base builds emitted the same
exact eleven-file artifact. PR #439's complete CI matrix passed on Python 3.12/3.13/3.14, including
Ruff, mypy, file lint, locked-SDD and Rulesync checks, the website job, the `ci-success` umbrella,
and CodeQL. The approved artifact was rebuilt and all five routes plus the SVG favicon returned HTTP
200 from the port-8766 preview with the expected `image/svg+xml` media type.

Definition of done: every page advertises the same local SVG favicon, the emitted icon contains the
selected A/G/W rocket mark without flames, and the deterministic site artifact contains exactly
eleven files at either supported site base.

## Phase 4G: continuous Lander expedition

- [x] Amend and review the FRD, HLA, this plan, and `brand-and-lander-lld.md` for one continuous
      forward expedition: seeded rolling terrain, elevated three-lander-width pads, single-building
      NOCs, carried fuel, progressive awards, repeated deployment, checkpoints, and vacuum crashes.
- [x] Delegate the LLD revision to an `agentworks-dev` subagent. It must pin the world seed and
      generator, minimum terrain diversity, rolling-window and camera boundaries,
      terrain/platform/building collision, stronger engine constants, site lifecycle, route-fuel
      proof and search resolution, difficulty curve, checkpoint semantics, DOM/SVG contract,
      animation timing, accessibility, reduced motion, deterministic vectors, and performance
      ceilings without changing completed plan records.
- [x] Lead-review the LLD against R7-R9/R21-R23, D7, AC5-AC8/AC18-AC19/AC22-AC24, the shared
      Lander/404 subtree, the no-framework constraint, and the in-memory-only privacy boundary;
      resolve every requirements or architecture conflict before implementation.
- [ ] Obtain pre-implementation `agentworks-reviewer` approval of the material SDD revision through
      the stacked draft PR; address every valid artifact finding and re-review before coding.
- [x] Delegate implementation to an `agentworks-dev` subagent with ownership of the pure world
      module, flight/mission model, controller, shared scene, Lander CSS, focused tests, browser
      checklist, builder manifest/validation, and permanent `website/README.md` updates. Isolate its
      worktree and preserve concurrent changes.
- [x] Add a pure seeded world module and split model/world tests so focused production and test
      files target the preferred 500-line scale and remain below the hard 1,000-line ceiling. Expand
      both deterministic site-base artifacts by exactly the one reviewed local module and retain one
      builder CLI, one scheduler, one controller, and one byte-equivalent shared game subtree.
- [x] Implement a bounded rolling SVG world with varied terrain, a stable camera transform, one
      slightly elevated pad exactly three lander widths long, one solid battery-style NOC, one
      consumable gas can, and a right-edge next-site cue; never append unbounded terrain, sites,
      debris, event queues, or DOM nodes.
- [x] Implement repeated safe-landing cycles, visible/programmatic fuel, stronger thrust authority,
      deterministic conservative route-fuel proofs, a ratio beginning near three and monotonically
      approaching one, exact excess carryover, single-consumption cans, powered-NOC retention, and
      post-deployment return to controllable flight.
- [x] Implement finite vacuum crash presentation and checkpoint recovery: compact flash,
      deterministic ballistic fragments, no atmosphere/audio/page movement, reduced-motion atomic
      failure, and restart at the exact last post-refuel pad without duplicating fuel or the can.
- [x] Add independent seeded-world, minimum-award, ratio, carryover, checkpoint, swept-contact,
      repeated-site, camera/window, arrow, fuel-accessibility, motion, lifecycle, and performance
      witnesses. Preserve the existing input-timestamp, frame-schedule, pause, no-JavaScript,
      privacy, local-reference, focus, reflow, and shared-fragment contracts.
- [ ] Update permanent game/demo documentation in lockstep with the implementation. Run focused and
      complete gates, deterministic root/project builds, current Chrome and Edge acceptance, and a
      long-run browser performance/memory audit with an explicit fixed-window witness.
- [ ] Obtain `agentworks-reviewer` and fresh-eyes code approval, resolve valid findings through the
      implementing subagent, mark the stacked PR ready, rebase it and change its base after PR #439
      merges, and monitor every required CI and CodeQL check.

Code-review evidence (2026-08-10): after three adversarial correction rounds, the project reviewer
and cold fresh-eyes reviewer approved exact head `559a7f8d` against merged `main` `0fe72d67` with no
findings. The final witnesses strictly compare 81 independently reconstructed world descriptors to
production, detect both arithmetic reassociation and a `0.15` to `0.14` relief mutation, and retain
byte-identical routes and demonstrated minima. The 32 Node, 114 website Python, and 7,753 complete
repository tests pass, as do Ruff, mypy, file lint, locked-SDD, Rulesync, deterministic 12-file
root/project builds, and diff checks. The final compound item remains open for Chrome/Edge and
long-run browser acceptance, saga-lead checkpoint closure, ready-state transition, and final forge
checks.

The project reviewer then approved exact runtime head `e0db7e22` after the browser-found DOM-ceiling
correction, confirming the static/dynamic SVG flattening, exact simultaneous 80-descendant witness,
CSS/state parity, no-JavaScript and ARIA contracts, shared-fragment identity, and module boundaries
with no finding.

Automated browser evidence (2026-08-10): isolated Chromium 151 acceptance at exact runtime head
`e0db7e22` passed three repeated service cycles, every named collision and lifecycle path, real
keyboard/mouse/touch input, hidden-tab freezing, request/storage privacy, camera reversal, and
static/dynamic visual parity. The first run correctly found 84 world descendants during ordinary
flight against the hard limit of 80; the implementation removed four redundant wrappers per site,
and the focused rerun rendered the true simultaneous worst case of ten terrain paths, three full
sites, eight debris fragments, and exactly 80 descendants. Active-frame p95 was `0.9 ms` against
`4 ms`; 100-site in-browser generation-plus-two-proof p95 was `7.2 ms` and maximum `15.3 ms` against
`25/50 ms`. After garbage collection, observed JavaScript heap and DOM-node counts decreased and
event-listener count remained fixed. The operator's current Chrome and Edge pass remains open; the
permanent checklist records exact methods, results, limitations, and cleanup.

Definition of done: the same accessible game on `/lander/` and `404.html` supports at least three
successive generated sites in one run, its demonstrated route allowance and decaying multiplier are
deterministic, fuel/checkpoint state cannot duplicate, crash and reduced-motion behavior are finite
and honest for a vacuum, and runtime memory/DOM work remains bounded independent of sites completed.

## Phase 4H: final Lander handling and world polish

- [x] Reproduce and measure the operator's six handling and visual observations against exact
      runtime head `f353e571`: identify the platform rectangle's rendered source, record current
      terrain frequency and amplitude, exercise the staged NOC power state, measure a real steering
      pulse and its post-release inertia, and probe every safe-contact boundary without editing
      code.
- [x] Amend the FRD, HLA, and this plan without rewriting completed Phase 4G history. Require a
      visibly supported elevated platform, one flat site shelf, coarse irregular terrain, a vertical
      multicolor power-up aimed toward the antenna, actual thrust vectoring with reduced axial
      force, light vacuum-honest flight-control stabilization, and a modestly relaxed landing
      envelope.
- [x] Delegate and lead-review an exact LLD amendment that pins platform support geometry, terrain
      sample and shelf construction, battery geometry/colors/stages, engine mixing and gimbal force,
      powered angular assist, safe-contact limits, deterministic vectors, route and world fixture
      regeneration, performance bounds, and browser acceptance before implementation.
- [x] Obtain an `agentworks-reviewer` approval of the amended LLD and resolve every valid artifact
      finding before changing production or fixture code.
- [x] Delegate implementation to an `agentworks-dev` subagent in an isolated worktree. Preserve one
      world authority and one immutable physics profile; update the model, controller projection,
      shared static scene, CSS, independent derivation tool, canonical route/world fixtures, focused
      tests, permanent documentation, and exact artifact witnesses atomically.
- [x] Prove every regenerated constructive route under the changed terrain, platform, force, assist,
      and landing profile, including collision-aware two-replay success and one-quantum exhaustion.
      Fail the effort rather than introducing runtime search, weakening deterministic bounds, or
      retaining stale route/world literals.
- [ ] Run focused Node and website suites, deterministic root/project builds, complete repository
      gates, module-size and bounded-DOM checks, then exercise the coherent preview in current
      Chrome and Edge. Browser acceptance must cover three sites, keyboard/vi/touch input, steering
      feel, steer release while neutral collective visibly stabilizes rotation, separate release-all
      vacuum coasting, safe/crash boundaries, coarse terrain and flat shelves, support appearance,
      every NOC stage, reduced motion, lifecycle/privacy, and long-run performance.
- [ ] Obtain fresh-eyes browser evidence and `agentworks-reviewer` code approval, resolve every
      valid finding through the implementing subagent, update the stacked PR evidence, rebase or
      merge the latest `main`, mark PR #486 ready, and monitor every required CI and CodeQL check.

Operator evidence (2026-08-10): the operator accepted the continuous-expedition direction and asked
for this final tuning pass after using the port-8766 preview. Isolated Chromium inspection at exact
head `f353e571` reproduced the pale rectangle as a `0.45 m` exposed sky slot beneath every platform,
not a transient node. It measured the current regular `4 m` terrain samples, left-to-right single-
color battery fill, one-engine steering lift, absence of linear or angular stabilization, and exact
`1.4 m/s`, `2.2 m/s`, `8 degree`, and `12 degree/s` safe-contact boundaries. A roughly half-second
steering pulse exceeded the angular limit and then coasted unchanged after release. These
measurements justify one coupled geometry, world, input-force, stabilization, and tolerance revision
rather than isolated CSS or threshold patches. Phase 4G's completed implementation and review
records remain historical evidence; PR #486 stays draft until every Phase 4H gate is complete.

Implementation evidence (2026-08-10): exact pushed head `da499872` centralizes the named steering
authorities, regenerates all nine constructive routes and all 81 ordered world descriptors through
the independent v3 deriver, and passes both exact success and one-quantum exhaustion replays without
runtime search. The final local gate passed 39 Node tests and 116 website Python tests; the complete
repository gate passed 7,753 non-integration Python tests, Ruff, formatting, mypy, file lint,
locked-SDD, Rulesync, deterministic 12-file root and project-path builds, and the module-size
limits. Independent project and fresh-eyes reviews approved the final implementation with no open
finding.

Automated browser evidence (2026-08-10): isolated Chromium 151 on exact served runtime `40ba11cd`
confirmed the solid elevated riser, coarse continuous terrain and exact collision seams, vertical
four-color battery stages, keyboard and pointer steering vectors, attached gimbaled plumes, precise
tap deadlines, three successive services, bounded retention and DOM, and the prior lifecycle,
privacy, reduced-motion, and performance contracts. A final behavior-preserving vector smoke on
exact runtime `da499872` observed a maximum plume-anchor error of `5.27e-7 px`, the exact `1.44`
axial ceiling, and no runtime exception. The browser process, profile, debug port, and scratch
evidence were removed after each run. Human Chrome and Edge handling and visual acceptance remains
open on the port-8766 preview.

Design-review evidence (2026-08-10): lead review found and removed a mixed keyboard/pointer merge
that could exceed straight collective thrust. Cold fresh-eyes review then found and closed four
artifact gaps: the production import DAG, synchronous lost-capture handling after a short tap, exact
ordering of all 81 world witnesses, and a nominally coarse but still repetitive terrain motif. The
final design uses four distinct seeded terrain profiles across every four consecutive chunks and
separates steer-release powered stabilization from release-all vacuum coasting. The project reviewer
and cold reviewer approved exact pushed head `b1cd82c7` with no findings after independently
recomputing every published physics/world vector, negative-index motif traversal, structural target
band and zero-delta termination, input ceilings, touch token ordering, and witness cardinality. A
100,000-seed probe observed no diversity or target-band failure. File lint, locked-SDD, Rulesync,
and diff gates passed. This is pre-implementation approval only.

Definition of done: the elevated pad has an intentional supported silhouette with no long pale slot;
every pad and NOC shares a flat shelf within coarse, stronger, irregular terrain; the NOC energizes
vertically toward its antenna in visible colored stages; steering vectors thrust without adding
axial lift above straight collective; neutral collective visibly counters residual rotation while
engine-off coasting and vacuum crash debris remain ballistic; the modestly relaxed landing envelope
and every regenerated route/world fixture are exact, reviewed, and browser-accepted.

## Phase 4I: deployment payoff, manual departure, and site structure

- [x] Measure the exact Phase 4H runtime before design changes: identify the uncapped fuel
      authority, current status/live-region projection, automatic launch transaction and checkpoint
      semantics, axial force under both steering modes, current platform clearance/collision
      envelope, retained DOM budget, and every route/fixture dependency that a force or geometry
      change invalidates.
- [x] Amend the FRD, HLA, and this plan without rewriting completed Phase 4H evidence. Require a
      leg-relative left-side vertical fuel gauge beside the exact numeric reserve, one accessible
      `Agent Deployed!` banner, player-commanded departure from a safely held powered pad,
      materially lower steering axial thrust, and one visibly elevated pad/NOC scaffold structure.
      Pin the exact new Lander and 404 copy plus the clean battery and symmetric network-signal
      direction.
- [x] Delegate and lead-review an exact LLD amendment that pins gauge semantics and geometry,
      deployment banner ownership, launch-ready state/input/restart behavior, fuel preservation,
      player-reachable proof prefix, vector-force targets, platform/scaffold collision and
      rendering, battery/signal geometry and timing, route/world regeneration, DOM bounds, and
      browser evidence.
- [x] Obtain an `agentworks-reviewer` approval of the amended LLD and resolve every valid artifact
      finding before changing production or canonical fixture code.
- [x] Delegate implementation to an `agentworks-dev` subagent in an isolated worktree. Update the
      shared fragment and page copy, world/model/controller/CSS projection, exact route derivation
      and fixtures, validators, focused tests, permanent README, and browser checklist as one
      coherent change. Preserve one state authority and one accessible status authority.
- [x] Prove all nine regenerated constructive routes and all ordered world witnesses after the force
      and structure changes, including the player-reachable launch prefix, exact two-replay success,
      one-quantum exhaustion, collision/render parity, no award duplication, and bounded
      runtime/DOM.
- [x] Run focused website suites, deterministic root/project builds, complete repository gates, and
      cold project/fresh-eyes reviews; resolve every valid finding through the implementing
      subagent.
- [ ] Refresh the port-8766 preview from the exact reviewed commit and exercise current Chrome and
      Edge across at least three deployments, the gauge and banner, manual wait/takeoff/restart,
      keyboard/vi/pointer/touch steering feel, scaffold appearance/collision, all battery/signal
      stages, reduced motion, lifecycle/privacy, and long-run performance.
- [ ] Merge or rebase the latest `main`, update PR #486's evidence and review comments, mark it
      ready only after operator acceptance, and monitor all required CI and CodeQL checks to
      completion.

Operator evidence (2026-08-10): after using the Phase 4H preview, the operator accepted its
substantial improvement and requested one cohesive payoff and structure round. The game must show a
left-side vertical fuel gauge, announce `Agent Deployed!`, wait for the player to depart a powered
pad, vector steering much more strongly away from forward lift, and present the pad and NOC as one
elevated trussed/scaffolded structure. The dedicated page heading becomes
`We need to deploy some agents!`; the 404 explanation becomes
`This route is broken! We need to deploy some agents!`. The battery loses its terminal nub, and a
vertically symmetric network signal provides the final three power stages.

Definition of done: fuel remains numerically exact and uncapped while its leg-relative gauge is
honest; each successful deployment produces one accessible payoff banner and a stationary,
fuel-preserving launch-ready checkpoint until the player thrusts; steering has materially less axial
force; pad, scaffold, NOC, collision, and static/dynamic projections agree; battery and symmetric
signal stages build upward; routes/world witnesses are atomically regenerated; and the result is
review-clean, browser-accepted, and merge-ready.

## Phase 4J: arcade presentation and persistent deployed agents

- [x] Measure the exact Phase 4I fragment, fuel/service/status projection, scene and terrain bounds,
      retained-site DOM budget, crash/action lifecycle, reduced-motion behavior, font/CSP policy,
      and current agent/NOC geometry before selecting an arcade presentation.
- [x] Amend the FRD, HLA, and this plan without rewriting completed Phase 4I evidence. Require a
      visual-only multicolor fuel gauge with hidden accessible numeric output, deterministic
      can-to-gauge refueling, one centered arcade banner authority for success and failure, an
      in-scene terrain-separated controls rail and state actions, and one persistent installed agent
      at every powered retained NOC.
- [x] Delegate and lead-review an exact LLD amendment that pins markup/source order, the local
      monospace arcade stack, fuel level/color/refill projection, reduced-motion and hidden-document
      behavior, banner and native-action geometry, bottom-rail/terrain separation, installed-agent
      model and path reuse, DOM bounds, focus/accessibility, and mutation-sensitive browser
      evidence.
- [x] Obtain an `agentworks-reviewer` approval of the Phase 4J artifact amendment and resolve every
      valid artifact finding before changing production code.
- [x] Delegate implementation to an `agentworks-dev` subagent in an isolated worktree. Update the
      shared fragment, model/controller projection, CSS, validators, focused tests, permanent
      README, and browser checklist as one coherent change. Preserve the sole status and fuel
      authorities, static/dynamic identity, and Phase 4I physics/route/world fixtures.
- [x] Prove deterministic refuel interpolation and transfer, reduced-motion equivalence, installed
      agent persistence across service/checkpoint/restart/retention, structural success/crash state
      and action focus, terrain-to-control-rail separation, narrow/zoom non-overlap, hidden-page
      pause, no font/network/storage additions, and the unchanged world ceiling and route digests.
- [x] Run focused website suites, deterministic root/project builds, complete repository gates,
      artifact/code/fresh-eyes reviews, and automated Chromium acceptance; resolve every valid
      finding and re-review the immutable integrated head.
- [x] Add an exact-zero whole-gauge red blink with paused and reduced-motion behavior; cover the
      model-to-DOM band, computed browser style, and static CSS contract without adding semantic or
      timing authority.
- [x] Triage the live PR timeline. Preserve the operator-superseded scope decisions and correct the
      remaining saga-lead finding: default-host interim publication stays useful, while
      custom-domain activation waits for both accepted Lander and canonical onboarding integration
      gates.
- [ ] Refresh port 8766 from the exact reviewed head and obtain operator Chrome and Edge acceptance
      of the arcade feel, fuel transfer, success/crash overlays, in-game controls/actions, and
      persistent agents across at least three deployments.
- [ ] Merge or rebase the latest `main`, update PR #486 and its evidence, mark it ready only after
      operator acceptance, and monitor every required CI and CodeQL check to completion.

Operator evidence (2026-08-11): the operator judged Phase 4I substantially improved and requested a
final cohesive visual round. Fuel becomes a stronger visual instrument without visible amount text,
with danger-to-ready color and an animated can transfer/refill. `Agent Deployed!` becomes a
centered, bordered arcade payoff; `Crashed!` receives the same treatment with Restart and Exit below
it. The small blocky controls legend moves inside the bottom of the scene while terrain remains
above it. The agent that plugs into a NOC remains visibly installed as the first persistent power-up
mark.

Implementation evidence (2026-08-11): integrated commit `765452fa` adds the shared arcade stage,
visual fuel/refuel projection, outcome and native actions, internal controls rail, and
installed-agent projection without changing the route, physics, world, or geometry digests. The
delegated exact-head gates passed 61 Node tests, 127 Python website tests, 7,754 non-integration
repository tests, strict typing and formatting, derivation verification, deterministic twelve-file
root/project builds, the world descendant budget, locked-SDD, Rulesync, and file lint. Commit
`8f999dee` adds the Phase 4J Node suite to both CI and Pages.

Review evidence (2026-08-11): code and fresh-eyes reviewers approved exact head `ed6d5f93` with no
open findings after `b6ae2318` made the direction description predicate exact, removed authored
status wording from banner state, and replaced Phase 4J prose pins with structural witnesses. The
broader isolated Chromium run passed 23 of 23 measured checks on the immediately preceding
integrated production head; exact-head Chromium then covered the corrected raw and resolved
accessibility relationship, while later commits changed documentation only. Automated evidence
includes three service cycles, exact refuel samples and resize projection, a real hidden-tab freeze,
normal and reduced-motion outcomes, installed-agent retention, native action isolation, 320-pixel
layout, a worst measured world count of 77 below 80, same-origin-only requests, empty storage, and
clean teardown. Human Chrome and Edge acceptance remains intentionally open.

Definition of done: R24 and AC25 hold without changing Phase 4I physics, collision, routes, world
generation, privacy, or shared-fragment identity; the arcade HUD communicates without a downloaded
font or color/motion alone; all refuel, banner, action, control-rail, persistent-agent,
reduced-motion, DOM, accessibility, and responsive witnesses are review-clean and browser-accepted;
and PR #486 is merge-ready.

The Phase 4J operator-acceptance and merge rows remain open because the operator reviewed that exact
preview and requested the cohesive Phase 4K refinement below before accepting the stacked PR. No
separate Phase 4J acceptance is expected.

## Phase 4K: self-documenting action rail and forgiving touchdown

- [x] Measure the exact Phase 4J action markup, controller projection, input and focus lifecycle,
      bottom controls rail, narrow layout, safe-contact constants, route fixture, and permanent
      browser evidence before choosing the refinement.
- [x] Amend the FRD, HLA, and this plan without rewriting completed Phase 4J evidence. Require one
      always-available rail Exit control with an Escape hint, one failure-only Restart control with
      an `r` hint, no dedicated Launch control, no duplicated shortcut prose, and a modest exact
      landing-envelope relaxation.
- [x] Delegate and lead-review the exact LLD amendment for DOM/source order, shortcut markup and
      accessible relationships, state/action visibility, focus and pointer isolation, responsive
      rail/overlay geometry, manual launch-ready departure, new landing boundaries, and atomic route
      derivation/fixture regeneration.
- [x] Obtain an `agentworks-reviewer` approval of the Phase 4K artifact amendment and resolve every
      valid artifact finding before changing production or canonical fixture code.
- [x] Delegate implementation to an `agentworks-dev` subagent in an isolated worktree. Update the
      shared fragment, CSS, controller/model projection, validators, route fixture and literals,
      focused tests, permanent README, and browser checklist as one coherent change.
- [x] Prove the exact four landing equalities and independent epsilon failures, all-nine
      constructive route derivation and one-quantum exhaustion, manual departure through
      keyboard/vi/pointer/touch, persistent Exit and failure-only Restart behavior, focus recovery,
      no action-to-flight input leakage, 320-pixel and 400-percent layout, shared-game identity, and
      unchanged world/DOM/privacy bounds.
- [x] Run focused website suites, deterministic root/project builds, complete repository gates,
      artifact/code/fresh-eyes reviews, and automated Chromium acceptance; resolve every valid
      finding and re-review the immutable integrated head.
- [ ] Refresh port 8766 from the exact reviewed head and obtain operator Chrome and Edge acceptance
      of the self-documenting buttons, action placement, manual takeoff, and more forgiving landing.
- [ ] Merge or rebase the latest `main`, update PR #486 and its evidence, mark it ready only after
      operator acceptance, and monitor every required CI and CodeQL check to completion.

Operator evidence (2026-08-11): after judging Phase 4J's arcade presentation substantially improved,
the operator requested one final control cleanup. Exit becomes a persistent bottom-right rail button
with a smaller Escape line; the controls legend therefore stops repeating Escape. Restart alone
appears beneath `Crashed!`, with a smaller `r` line, and the legend stops repeating `r`. The
dedicated Launch button is removed so `Agent Deployed!` stands alone and the player departs with
ordinary game controls. The safe-contact limits rise modestly to `1.8` horizontal, `2.8` descending,
`12` degrees tilt, and `18` degrees per second rotation.

Implementation evidence (2026-08-11): integrated commit `f70315c2` deletes the Launch authority,
makes Exit the persistent rail action, makes Restart failure-only, scopes physical keys to the
active non-interactive game path, exposes separate fuel label/value descriptions, and regenerates
the landing proof under the exact `1.8/2.8/12/18` envelope. Geometry and world digests remain
unchanged; physics/output digests change as designed, and only the comparator-selected route-93
terminal runs change. Commit `2bd99b7d` adds the Phase 4K Node suite to both workflows.

Review evidence (2026-08-11): code and fresh-eyes reviewers approved exact head `39f1dd8d` after
validator and browser-witness hardening. Final gates pass 69 Node tests, 140 Python website tests,
7,754 non-integration repository tests, strict typing and lint, deterministic twelve-file root and
project builds, locked-SDD, and Rulesync. Chromium exercises real keyboard, vi, mouse, and touch
departure; native action focus and single activation; the complete outside-shell key matrix; exact
numeric accessibility refresh; listener-removal mutations; responsive layout; hidden/reduced motion;
shared Lander/404 behavior; and acquisition-failure cleanup. Human Chrome and Edge acceptance
remains intentionally open.

Definition of done: R23-R24 and AC24-AC25 describe one unambiguous action hierarchy; launch-ready
has no action besides persistent rail Exit, failed adds only Restart, shortcut hints are visible and
accessible without duplicated legend copy, every input path still initiates manual departure, the
revised exact envelope and regenerated proof artifacts are deterministic, and the immutable result
is review-clean, browser-accepted, and merge-ready.

The Phase 4K operator-acceptance and merge rows remain open because the operator reviewed that exact
preview and requested the cohesive Phase 4L refinement below before accepting the stacked PR. No
separate Phase 4K acceptance is expected.

## Phase 4L: two-line controls, checkpoint retry, and final landing tolerance

- [x] Inspect the exact controls-rail markup/CSS, checkpoint serialization/restoration, retry focus
      lifecycle, safe-contact constants, and route-proof authorities before selecting the
      refinement.
- [x] Amend the FRD, HLA, and this plan without rewriting completed Phase 4K evidence. Require one
      non-wrapping keyboard instruction line followed by one touch instruction line, exact `Retry`
      action naming with the existing `r` hint, exact powered-pad pose/fuel restoration, a second
      bounded landing-envelope relaxation, and native varied terrain beneath every elevated site.
- [x] Delegate and lead-review an exact LLD amendment for instruction markup and responsive layout,
      Retry label/accessibility/focus/state behavior, checkpoint snapshot identity, one continuous
      alternating-triangle platform-to-NOC truss, exactly three left/center/right terrain-reaching
      supports and honest colliders, monotonic seam-free terrain projection, exact new contact
      boundaries, the one-indexed `1 + 0.5^(n-1)` refuel ratio, and atomic route/world/geometry
      derivation and fixture regeneration.
- [x] Obtain an `agentworks-reviewer` approval of the Phase 4L artifact amendment and resolve every
      valid finding before changing production or canonical fixture code.
- [x] Delegate implementation to an `agentworks-dev` subagent in an isolated worktree. Update the
      shared fragment/CSS, world geometry and projection, model proof constants and literals,
      validators, focused tests, permanent README, and browser checklist as one coherent change.
- [x] Prove keyboard instructions occupy the first rail line and touch instructions the second
      without internal wrapping at the accepted layouts; Retry retains `r`, restores the exact
      checkpoint site/pose/fuel/progress without duplicate award, and returns focus to the scene;
      the complete pad-to-NOC scaffold has one continuous alternating-triangle rhythm, three
      collision-backed supports reach the untouched varied terrain, and terrain rendering has no
      duplicate-position or vertical-seam artifact; refuel ratios are exactly
      `2, 1.5, 1.25, 1.125, ...` by powered-base number and carried excess remains additive;
      `2.0/3.2/15/22` equality contacts are safe while independent epsilon excesses crash; all nine
      constructive routes, world witnesses, and one-quantum failures regenerate deterministically.
- [x] Run focused website suites, deterministic root/project builds, complete repository gates,
      artifact/code/fresh-eyes reviews, and automated Chromium acceptance; resolve every valid
      finding and re-review the immutable integrated head.
- [ ] Refresh port 8766 from the exact reviewed head and obtain operator Chrome and Edge acceptance
      of the two-line controls, Retry semantics, exact checkpoint restoration, continuous truss,
      three terrain-reaching supports, seam-free varied terrain beneath sites, and landing feel.
- [ ] Merge or rebase the latest `main`, update PR #486 and its evidence, mark it ready only after
      operator acceptance, and monitor every required CI and CodeQL check to completion.

Operator evidence (2026-08-11): the operator judged Phase 4K very close and requested four final
refinements. Keyboard instructions occupy the first rail line and touch instructions the second. The
crash action is named `Retry`, retaining the `r` hint, because it restores the last powered pad
rather than restarting the expedition. That restoration must preserve the checkpoint's exact
centered pose and post-award carried fuel. Safe contact rises one more bounded step to `2.0`
horizontal, `3.2` descending, `15` degrees tilt, and `22` degrees per second rotation. The differing
pad, connector, and NOC X-brace fields become one visually continuous open structure with a uniform,
more expressive alternating-triangle truss rhythm. The multiplier between demonstrated minimum fuel
and each can's award is named the refuel ratio and becomes exactly `1 + 0.5^(n-1)` for one-indexed
powered-base number `n`, starting at `2` and approaching one.

The operator then rejected the remaining flat-site shelf in favor of the native random terrain
continuing beneath each site. The continuous pad-to-NOC truss stays level, while exactly three
visible supports at its left, center, and right reach the independently varied surface. This must
also eliminate the terrain's vertical-line artifacts by keeping one ordered, continuous terrain
chain through every site and chunk boundary rather than splicing a horizontal replacement into it.

Artifact-review evidence (2026-08-11): an independent `agentworks-reviewer` approved exact LLD head
`09602c0d` with no findings after reproducing all three integer deck levels, structural template
termination, the 17-member truss and three native-foot colliders, all aperture bounds, four 100-site
generation vectors, 81 strict-terrain world witnesses, 36 constructive candidates, 162 selected
replays, byte-identical route records, and all four regenerated digests. File lint, locked-SDD,
Rulesync, and diff checks passed.

Implementation and automated-review evidence (2026-08-11): exact integrated head `c9825a04`
implements native site terrain, a shallow continuous Warren truss, three full-length
deck-to-native-foot pylons, split fill/open-surface terrain projection, two controls rows, Retry,
the `2.0/3.2/15/22` landing envelope, and the direct refuel-ratio sequence. Cold review found and
closed a shortened-pylon projection, a degenerate strict-X range, missing Phase 4L
controller/browser witnesses, and authored-prose test pins. The corrected ordinary derivation
verifies 81 world descriptors, 36 candidates, 162 selected replays, byte-identical route records,
geometry digest `2cc7b145...14195`, physics digest `34a7cb64...92e`, world digest `c191e4ae...4995`,
and output digest `239a33c5...48b9`. Final automated evidence passes 70 Node tests, 142 website
Python tests, 7,754 repository tests, deterministic root/project builds, file lint, locked-SDD,
Rulesync, workflow, and diff gates. Two independent reviewers approved the exact immutable head with
no finding. Isolated headless Chromium 151 showed no vertical terrain seam while moving forward and
backward, exact three-pylon terrain contact, two non-wrapping controls rows at 320 CSS pixels and
400-percent equivalent, exact twice-repeated checkpoint Retry, 75 maximum world descendants, and
100-site bounded generation. This is automated Chromium evidence only; operator Chrome and Edge
acceptance remains open.

Definition of done: controls have deterministic two-line layout without awkward internal wrapping;
Retry's name and behavior agree; checkpoint restoration is exact and mutation-protected; the whole
pad-to-NOC span reads as one honest continuous structure supported at left, center, and right over
uninterrupted varied terrain; terrain projection has no vertical seam; the refuel ratio and
carried-excess award are exact; revised contact boundaries and all derived proof artifacts are
deterministic; and the immutable result is review-clean, browser-accepted, and merge-ready.

## Phase 4M: free exploration, lattice supports, and a living sky

- [x] Inspect the exact support/truss projection, initial fuel and gauge authorities, camera and
      horizontal failure bounds, target cue, deployment timing, static sky, shared copy, and footer
      route/name contract before selecting the refinement.
- [x] Amend the FRD, HLA, and this plan without rewriting completed Phase 4L evidence. Require three
      lattice columns integrated with the Warren truss, an exact half-gauge initial reserve, bounded
      deterministic parallax stars and occasional celestial landmarks, bidirectional exploration
      without artificial horizontal crashes, half-duration pre-NOC agent travel, shortened page
      copy, and a direct fragment-free Lander footer destination with new hover/accessibility text.
- [x] Delegate and lead-review exact LLD amendments for both the shared site shell and Lander. Pin
      column member geometry, honest colliders and aperture bounds; initial fuel feasibility and an
      honestly named display reference; sky generation, parallax, static fallback, and DOM limits;
      two-way camera/retention/cue behavior; removal of horizontal-position failure while preserving
      collision, ceiling, and excessive-speed safety outcomes; deployment timing; copy/route
      projection; fixture/digest impact; and mutation-sensitive browser evidence.
- [x] Obtain an `agentworks-reviewer` approval of the Phase 4M artifact amendments and resolve every
      valid finding before changing production or canonical fixtures.
- [x] Delegate implementation to an `agentworks-dev` subagent in an isolated worktree. Update the
      shared shell and game fragment, CSS, world/model/controller modules, independent derivation
      and fixtures when required, validators, focused tests, permanent README, and browser checklist
      as one coherent change without prose-policing unit assertions.
- [x] Prove a fresh run begins at exactly half of its visual reference and the first demonstrated
      landing remains feasible; later awards/checkpoints still project full without capping carried
      excess. Prove the lander can pass the target and both former horizontal bounds, reverse, and
      return without failure while camera, retained terrain/sites, cue direction, collision, fuel
      exhaustion, and teardown remain deterministic and bounded.
- [x] Prove all three supports render as fixture-derived lattice columns joined to the Warren truss,
      reach independent native-terrain feet, contain every member pixel inside honest colliders, and
      retain bounded aperture/DOM guarantees. Prove deterministic stars pan at the pinned parallax
      rate and occasional celestial landmarks enter/leave a bounded decorative layer with no
      requests or accessibility exposure.
- [x] Prove normal agent travel reaches the NOC in half the Phase 4L time while refuel, battery,
      signal, reduced-motion, hidden-time, installed-agent, checkpoint, and power-stage behavior
      stay unchanged. Review the shortened Lander/404 copy and footer hover text manually while
      automated tests enforce only structure, nonempty live-derived accessible names, and the direct
      fragment-free `/lander/` route.
- [x] Run focused website suites, deterministic root/project builds, complete repository gates,
      artifact/code/fresh-eyes reviews, and automated Chromium acceptance; resolve every valid
      finding and re-review the immutable integrated head.
- [ ] Refresh port 8766 from the exact reviewed head and obtain operator Chrome and Edge acceptance
      of the lattice supports, half-tank opening, free exploration, parallax sky, doubled deployment
      travel speed, shortened copy, direct Lander route, and footer hover text.
- [ ] Merge or rebase the latest `main`, update PR #486 and its evidence, mark it ready only after
      operator acceptance, and monitor every required CI and CodeQL check to completion.

Operator evidence (2026-08-11): after approving the native terrain and continuous shallow truss, the
operator requested more expressive truss-integrated lattice columns; a half-gauge initial reserve;
stars that travel with the landscape plus occasional celestial variety; unrestricted horizontal
exploration without missing-target failure; twice-fast pre-NOC agent travel with unchanged power-up
timing; shorter `We need agents!` page copy; a direct `/lander/` footer destination; and the hover
text `Help deploy some agents!`.

Operator evidence (2026-08-12): the operator requested that exact fuel exhaustion make the visual
gauge blink and asked for a current PR-feedback audit. The live PR has no formal review or inline
review comments. Its older proportionality objections were explicitly superseded by the operator;
the remaining valid saga-lead feedback requires both Lander and onboarding acceptance before custom
domain cutover.

Implementation and automated-review evidence (2026-08-11): exact runtime head `06cfd05a` and its
production-identical final documentation/test head `194cee21` implement three integrated open
lattice columns, a `15/30` half-gauge opening, deterministic `.24` parallax sky with 20 stars and
one or two celestial landmarks, unrestricted horizontal exploration with reversible camera and cue,
`.9 s` pre-NOC travel, shortened page copy, and direct footer navigation. Independent v5/v4
derivation evaluates 36 candidates, 162 selected replays, and 81 world witnesses; all nine
route/failure records and physics digest remain unchanged. The independently enumerated clear-face
maximum is `3.1894356867634124 m`, below the `3.2 m` hull, with geometry digest `a5120d97...e7ca`,
world digest `c666bb42...1e32`, and output digest `628c3562...b595`. Final gates pass 79 Node tests,
143 website Python tests including real Chromium, 7,754 repository tests, deterministic root/project
builds, lint, locked-SDD, Rulesync, and diff checks. Cold code and fresh-eyes reviews approve with
no remaining finding. Isolated Chromium traversed `x=+/-10000`, reversed and returned, reconstructed
the seed-correct sky, retained at most five chunks/three sites, and completed a 100-site run at
render p95 `.4 ms` and generation p95 `10 ms`. This is automated Chromium evidence only; human
Chrome and Edge acceptance remains open.

Definition of done: the opening fuel presentation is honest and playable; horizontal position alone
never crashes the lander while collision, ceiling, and excessive-speed safety outcomes remain;
both-direction exploration, camera, cue, world retention, and parallax remain bounded; each support
is an honest integrated lattice column; deployment travel alone is twice as fast; shared copy and
direct routing are correct; derivation and fixtures remain independent and deterministic; and the
immutable result is review-clean, browser-accepted, and merge-ready.

## Phase 4N: final tolerance and celestial polish

- [x] Raise the inclusive safe-contact limits by the same increments as the preceding tolerance
      adjustment: horizontal speed `2.0→2.2`, descent `3.2→3.6`, tilt `15→18`, and angular speed
      `22→26`. Regenerate the v4 derived fixture through the ordinary v5 tool and stop if any route,
      world descriptor, geometry digest, or proof cardinality changes.
- [x] Keep the NOC mast and antenna head fixed graphite in every power state while preserving the
      existing three signal-arch colors, sequence, and timing. Replace the planet's disconnected
      partial ring strokes with one complete closed ellipse and retain the existing deterministic
      crescent, sky cadence, parallax, DOM bound, and non-semantic projection.
- [x] Add mutation-sensitive model, CSS, and sky-geometry coverage; update the permanent README and
      manual browser checklist; and reconcile the FRD, HLA, and LLD without changing historical
      completed-phase evidence.
- [x] Run ordinary generation/verification, focused and full website suites, deterministic builds,
      repository gates, code review, fresh-eyes browser review, and update PR #486 only after the
      immutable head is clean.

Operator evidence (2026-08-12): after approving the Phase 4M parallax sky, the operator requested
one more tolerance increase, a permanently black fixed NOC antenna with unchanged radiating signal,
and a complete realistic-looking ring around the occasional planet.

Definition of done: the four inclusive touchdown boundaries are exact and independently fail at one
epsilon beyond; canonical geometry/world/routes remain unchanged while physics/output regenerate;
the fixed antenna never inherits signal color; the planet has one complete closed ring and no
partial quadratic artifact; and the reviewed build is deterministic and gate-clean.

## Phase 4O: occluded planetary rings

- [x] Replace the fully visible planet ring with exact foreground and exposed rear-side arcs whose
      rear center is hidden by the planet. Add deterministic restrained profiles for a compact
      single ring, a wider single ring, and a modest double ring without changing sky cadence,
      parallax, node count, semantics, or route fixtures.
- [x] Add independent geometry coverage for all three profiles and update the permanent sky guidance
      plus FRD, HLA, and LLD contracts without changing historical completed-phase evidence.
- [x] Run focused/full suites, deterministic builds, repository gates, code review, and fresh-eyes
      browser review; update PR #486 only after the immutable head is clean.

Operator evidence (2026-08-12): after reviewing the complete ring, the operator requested realistic
occlusion behind the planet and modest deterministic variation in ring count and diameter.

Definition of done: every generated ring has an exact foreground arc and no visible rear-center
segment; planets use only the three restrained one/two-ring profiles; bounded deterministic sky
behavior remains unchanged; and the immutable result is review-clean and gate-clean.

## Phase 4P: broad lunar relief

- [x] Amend `brand-and-lander-lld.md` with one exact deterministic relief kernel, the normalized
      `[0.1,0.6]` coordinate conversion, independently computable grade/grade-change/reversal
      limits, and exact static/runtime parity. Pin a coherent deck, route, ceiling, and
      vertical-projection design that keeps all sites reachable and the active vehicle visible; stop
      with an exact infeasibility witness rather than flattening terrain, weakening collision, or
      hiding relief in a presentation-only offset.
- [x] Independently review the amended FRD, HLA, plan, and LLD before implementation. Prove the
      chosen design across canonical route worlds, representative low/high terrain witnesses, and
      four 100-site runs without changing physics or proof bounds merely to preserve old literals.
      In addition to sampled evidence, prove for every seed that bounded site selection terminates
      and that every allowed deck transition has a collision-safe, below-ceiling constructive route;
      otherwise define and surface the exact infeasibility outcome rather than retrying forever.
- [x] Implement the reviewed world/model/controller/static-scene contract through an
      `agentworks-dev` delegation. Regenerate the geometry/world/output fixture evidence atomically,
      preserve one strict terrain authority for rendering/collision/support feet, and update
      permanent README/checklist guidance in the same change.
- [x] Add mutation-sensitive Node, Python, and real-browser witnesses for exact normalized bounds,
      near-bound extrema, realistic grade/curvature/reversal limits, peak/canyon silhouettes,
      static/dynamic parity, support/collision alignment, route replay, camera visibility, bounded
      retention, responsive layout, reduced motion, privacy, and deterministic reconstruction.
- [x] Run ordinary derivation/verification, focused and full website suites, deterministic builds,
      complete repository gates, code review, fresh-eyes browser review, and exact-head CI/CodeQL.
      Refresh port 8766 and update PR #486 only after the immutable head is clean.

Operator evidence (2026-08-12): the operator requested substantially taller terrain occupying the
normalized `0.1` through `0.6` scene band, with peaks and canyons but realistic, non-jagged
inflection.

Definition of done: native terrain visibly spans the requested vertical band without sawtooth
chatter; rendering, collision, support feet, sites, routes, ceiling, camera, and static recovery use
one deterministic authority; every canonical proof and long-run generation witness remains valid;
and the reviewed browser artifact is deterministic, bounded, and gate-clean.

The operator's 2026-08-12 hands-on review rejected this implementation despite its automated gates:
terrain appeared locally flat, the global deck placed every structure far above the ground, and the
vertical camera introduced scrolling. Completed Phase 4P records remain immutable history, but its
shipping design is superseded by Phase 4Q and must not be presented as accepted.

## Phase 4Q: terrain recovery and gameplay-scale relief

- [x] Restore the exact pre-Phase-4P terrain, deck, route, camera, static-scene, fixture, and test
      behavior as a green checkpoint atop the merged canonical onboarding release. Preserve all
      accepted Phase 4M-4O refinements and the thirteen-file website artifact.
- [x] Amend and independently review `brand-and-lander-lld.md` for gameplay-scale deterministic
      peaks and valleys, bounded realistic slope/grade change, fixed vertical projection, and the
      lowest collision-safe per-site deck derived from each local terrain envelope. Explicitly ban a
      global deck datum, vertical camera, and vertical overflow.
- [x] Implement the reviewed relief through `agentworks-dev`, regenerate fixtures and route proofs
      atomically, and add mutation-sensitive Node/Python/browser evidence for local deck minimality,
      normal-window silhouettes, collision/support parity, fixed scene height, and zero vertical
      scrolling.

The operator's 2026-08-13 hands-on review accepted the fixed-scene recovery but rejected Phase 4Q's
curved, insufficiently varied terrain and terrain-coupled site cycle. Completed Phase 4Q work
remains immutable history; Phase 4R replaces only terrain generation, terrain-derived deck/support
geometry, and the proofs and permanent documentation that necessarily consume them.

## Phase 4R: straight-segment lunar terrain

- [x] Amend and independently review `brand-and-lander-lld.md` for a strict-X straight-segment
      terrain authority whose complete surface remains in normalized `[0.1,0.6]`, visibly exercises
      that band in ordinary gameplay, and retains bounded navigable grade and grade change.
- [x] Prove horizontal site placement is selected without terrain-height input, then pin the deck
      exactly `2.5 m` above the independently reconstructed maximum terrain height beneath the full
      closed platform-to-NOC footprint; extend the existing integrated supports to native terrain
      without an artificial height cap.
- [x] Implement only the reviewed terrain, terrain-derived structure geometry, route fixtures, and
      their required validators, tests, and permanent docs. Do not change controls, vehicle physics,
      landing tolerance, fuel consumption, refuel ratio, mission copy, sky, power-up behavior, or
      any other presentation. The route-proof allowance may add only the operator-authorized,
      conservative positive-altitude term to its independently certified base; it gives descents no
      credit and does not manufacture a one-quantum-smaller route failure.
- [x] Run derivation feasibility, complete website/repository gates, independent code review, and
      fresh-eyes browser review. Prove collision/render/static parity, forward/reverse determinism,
      fixed `25/16` projection, and zero vertical scrolling at all required viewport sizes.
- [ ] Refresh port 8766 only after the exact reviewed head and exact-head CI/CodeQL are green, then
      obtain operator hands-on acceptance before marking Phase 4R complete.

Definition of done: the actual game shows angular straight-line lunar terrain spanning the requested
vertical band, every platform is placed independently and then lifted exactly 2.5 m above its own
under-footprint maximum, supports reach native terrain, no unrelated behavior changes, and the
operator accepts the hands-on result with no vertical scrolling.

The operator's 2026-08-14 hands-on review accepted the straight terrain, local decks, supports, and
fixed scene but rejected the forced alternating hill/valley rhythm and the remaining synthetic
ceiling and excessive-speed failures. Completed Phase 4R records remain immutable history; Phase 4S
changes only terrain variety and the two non-contact failure classifications.

## Phase 4S: free flight and less-periodic straight terrain

- [ ] Amend and independently review `brand-and-lander-lld.md` for a deterministic global
      straight-vertex authority without mandatory high/low alternation or a short repeating
      silhouette. Retain normalized `[0.1,0.6]`, exact grade/grade-change bounds,
      terrain-independent site positions, local `max+2.5 m` decks, native support feet, and zero
      vertical scrolling.
- [ ] Delete the vertical-ceiling crash and replace the 64-slice excessive-speed failure with a
      bounded, complete swept-collision traversal. Fuel depletion must suppress thrust without
      failing until a real terrain or structure contact occurs. A finite world is permitted only
      with a visible physical terminus whose contact causes the failure; no retained-window edge or
      pose within generated terrain may fail as a boundary.
- [ ] Regenerate terrain/world/route fixtures and proof projections atomically as required. Prove
      every concrete route geometry, opening, Retry checkpoint, static scene, and retained-world
      bound against the new terrain without changing controls, physics, landing tolerance, fuel,
      mission pacing, sky, site geometry, copy, route spacing, or route behavior. Generated
      reference schedules may change only as required to certify the new terrain geometries.
- [ ] Add mutation-sensitive model, world, derivation, static, and real-browser witnesses for former
      ceiling and excessive-speed crossings, zero-fuel ballistic contact, less-periodic seeded
      angular terrain, exact height/grade limits, local decks/supports, collision/render parity, and
      no scrolling at every required viewport.
- [ ] Run full website and repository gates, independent code and fresh-eyes browser review, refresh
      port 8766 only at an exact reviewed green head, and obtain operator hands-on acceptance before
      marking Phase 4S complete.

Definition of done: the lander can fly past its target throughout the known world while fuel and
actual collisions permit, empty fuel leads to ballistic descent rather than a synthetic explosion,
and any finite world edge is a visible contact-backed structure rather than a random failure. The
terrain is visibly less predictable while remaining straight, bounded, navigable, site-independent,
locally supported, and free of vertical page scrolling.

## Phase 5: CI and default Pages deployment

- [x] Delegate workflow implementation to an `agentworks-dev` subagent with ownership of the Pages
      workflow and the website job in existing CI. It must preserve existing workflow conventions
      and the `ci-success` umbrella.
- [x] Verify current stable major releases of official checkout, configure-pages,
      upload-pages-artifact, and deploy-pages actions at implementation time and pin them.
- [x] Add the website build/test job to pull-request CI and to `ci-success` so branch protection
      cannot omit it.
- [x] Add the Pages workflow: build and upload from a clean checkout, deploy on `main`, least
      permissions, `github-pages` environment, and safe concurrency.
- [x] Keep the publishing workflow free of path filters so authoritative inputs outside `website/`
      always rebuild and verify the deployed artifact.
- [x] Ensure pull requests exercise the build without deploying or acquiring Pages write/OIDC
      permissions; verify the uploaded artifact contains only intended files and identifies the
      source commit.
- [x] Before merge, obtain operator approval to enable GitHub Actions as this repository's Pages
      source and restrict the `github-pages` environment to `main`; record the non-secret setting
      evidence in `website/README.md` and PR #439.
- [x] Run workflow syntax checks, focused site tests, full repository CI-equivalent gates,
      locked-SDD checks, and file-quality lint.
- [x] `agentworks-reviewer` and a fresh-eyes reviewer inspect permissions, triggers, artifact
      boundaries, and failure modes; resolve valid findings and re-review.
- [x] Mark PR #439 ready only when the complete interim release is review-clean. Triage Copilot
      comments, record the five-commit missing-session-trailer provenance exception, request the
      saga lead's standalone-effort review, and resolve valid findings. Re-run current Chromium
      no-JavaScript recovery, source-order focus, Escape/browser-key independence,
      320-pixel/400-percent reflow, computed shared-shell visibility, reduced motion, and
      clean-context comprehension. Then mark the reviewed PR ready for the operator's Chrome and
      Edge pre-merge pass and merge decision. Firefox/WebKit, spoken screen-reader, physical mobile
      touch/human-motion, and broader device acceptance are post-launch gates for the final
      production-acceptance record, not blockers for this interim-release merge.

Final readiness evidence (2026-08-10): the operator accepted the forwarded preview in current Chrome
and Edge at `3bad34ab`. The final requested implementation delta at `a6f6522d` changes only TOC font
sizing, the canonical README/Home emphasis, and three behavior-neutral review cleanups. It passes
114 Python website tests, 22 Node model/controller tests, computed Chromium geometry, and exact
eleven-file root and project-base builds. All PR timeline comments and review records were triaged:
the blanket `email` word veto, dead `Block.markdown`, and dead build re-export were removed; the
separately withdrawn Home-projection, dual URL-boundary, and exact Security-reference findings
remain intentionally unchanged. The five historical trailer-less commits retain their recorded
session provenance. This final draft handoff is approved to transition directly to ready after the
evidence commit, PR-body refresh, and scoped exact-head comment, with no further source push.

- [ ] After merge, verify the matching commit deploys automatically at the default Pages URL. Leave
      custom-domain, onboarding, and lock boxes unchecked.

Definition of done: R3 and AC2 are implemented as a least-privilege automatic pipeline, and the
interim artifact is merged and live at the default Pages URL without a routine manual publish step;
Phase 9 owns the remaining production acceptance and closeout work.

## Phase 6: superseded interim activation

The operator's 2026-08-10 integration ruling superseded this unexecuted interim-only phase before it
began. Phase 2 and Phase 9 retain the durable production browser, accessibility, and device
acceptance obligations. Availability-notice, no-bootstrap, and unpublished-onboarding checks no
longer describe a possible shipped state.

## Phase 7: canonical assistance pickup and contract reconciliation

The operator's 2026-08-10 thin-bootstrap correction superseded the unexecuted plan to publish the
startup disclosure in the website prompt. PR #480 intentionally carried the canonical source and its
website consumer together. The website projects only the thin install, verification, and guide
handoff; the installed guide owns disclosure and source review.

- [x] Check the website feature directory and latest `main` before starting, preserving the merged
      Manifesto source-path change.
- [x] Inspect the canonical assistance source, generator, README block markers, tests, and permanent
      docs at the integrated feature head; consume the authored source rather than a harness
      wrapper.
- [x] Reconcile the FRD, HLA, and shell LLD to the thin-bootstrap placement, exact
      `docs/manifesto.md` source, and four permanent website content inputs.
- [x] Pin exact source paths, README fence semantics, byte/newline rules, builder inputs and errors,
      notice removal, semantic bootstrap DOM, copy states, and the complete-release test matrix in
      the existing shell and bootstrap-packaging LLDs without adding a second integration design.
- [x] Lead reviews the contract against R1/R2, C1/C3/C5, D3/D6/D10, and the current onboarding
      contract.
- [x] `agentworks-reviewer` reviews the implementation contract; valid findings are resolved and
      re-reviewed.

Definition of done: the final content seam is pinned at file and symbol level against current code,
with no branch-only dependency, duplicated bootstrap, or website-owned continuing-assistance prose.

## Phase 8: canonical assistance integration

- [x] Implement the main-page assistance projection, copy enhancement, focused tests, and permanent
      documentation in an isolated lane based on current `main`.
- [x] Delete the interim notice and make the canonical bootstrap a required builder input; enforce
      byte identity across source, README, and decoded built HTML, including newline and fence-edge
      cases.
- [x] Add the real semantic `pre`/`code` region and progressive copy behavior with accessible
      success/failure feedback and a fully usable no-JavaScript path. Do not change the established
      URLs, information architecture, visual system, 404, hosting, or DNS.
- [x] Invert interim guards: forbid the availability notice and require canonical bootstrap markup,
      identity, thin-scope semantics, copy behavior, and absence of website-owned source-review,
      startup-disclosure, or security-posture prose.
- [x] Run focused tests, full CI-equivalent gates, locked-SDD checks, and file-quality lint.
- [x] `agentworks-reviewer` and a fresh-eyes reviewer inspect content identity, accessibility,
      migration cleanliness, and scope; resolve valid findings and re-review.
- [x] Apply the operator's 2026-08-12 prose-test simplification: remove the website-owned semantic
      phrase guard while retaining canonical byte identity, DOM structure, copy behavior, and
      accessibility coverage. Authored bootstrap wording remains review-owned.
- [ ] Open the integration PR ready for review, triage Copilot comments, request the saga lead's
      review, resolve all valid findings, and hand the reviewed PR to the operator for merge.
- [ ] Verify the matching commit deploys automatically to production and that the interim notice is
      absent.
- [ ] Reverify the five-page shared shell, footer Lander link and target size, exact `/lander/`
      metadata/CSP, byte-equivalent Lander/404 game subtree, and compact 404 title after onboarding
      integration.

Definition of done: R1-R25 and R28-R29, AC2-AC3, and AC5-AC26 plus AC29-AC30 hold through the
existing site and pipeline, with the temporary notice removed rather than retained as configuration.

## Phase 9: complete acceptance and closeout

- [ ] Start the closeout branch from `main` and verify the accepted Lander plus onboarding artifact
      is the production deployment at the apex.
- [ ] Verify production HTTPS, certificate, apex metadata, `www` redirect, permanent links, no
      runtime third-party requests, and byte-identical copied bootstrap.
- [ ] Run human acceptance from a clean context: a newcomer understands the product and hands the
      block to an agent in under one minute. Record timing and any unexplained intervention.
- [ ] Run complete accessibility acceptance: keyboard-only flow, visible focus, landmarks/headings,
      copy status, 320 CSS pixel reflow, 400 percent zoom, contrast evidence, reduced motion, screen
      reader, and physical touch behavior. Complete the production compatibility pass in Firefox and
      Safari/WebKit on at least one additional device outside the pre-merge machine.
- [ ] Reverify the production `/lander/`, footer rocket, shared Lander/404 game subtree, and custom
      404 contracts and confirm the onboarding change did not disturb them.
- [ ] Reverify the home/security information architecture, non-preachy security path, sourced
      security claims, and shared terminal/TUI-derived visual system after onboarding integration.
- [ ] Record dated acceptance evidence without secrets or account tokens and promote every
      load-bearing operating fact to `website/README.md`; no permanent file depends on this SDD.
- [ ] Create `locked.md` with the final state and date only after all acceptance criteria are true;
      run final gates, obtain the required closeout review, and hand the ready PR to the operator
      for merge.

Definition of done: AC1-AC26 and AC29-AC30 hold at `https://agentworks.build`, R1-R25 and R28-R29
plus C1-C5 are production facts, the deployment is operable without this SDD, all plan boxes are
truthful, and the effort is locked.

## Escalation triggers

- The canonical assistance source or README parity seam changes so the website would need to parse a
  generated harness wrapper or maintain a second prompt.
- The canonical website prompt grows source-review, startup-disclosure, security-posture, or
  operating guidance instead of handing those concerns to `agw guide --agent`.
- GitHub Pages policy no longer permits the intended project site, or the site gains a commercial
  transaction/SaaS purpose.
- The repository or organization plan cannot enable Pages or a custom domain.
- GoDaddy contains records whose purpose is unclear and whose removal could affect mail or another
  service.
- GitHub cannot provision a valid apex and `www` certificate after documented propagation windows.
- Meeting the content or visual acceptance criteria requires a framework, backend, external asset,
  analytics, or scope beyond the FRD.
