# Plan: The agentworks.build Website

- Status: Implementation authorized; PR held for the complete site
- Date: 2026-08-07
- Last revised: 2026-08-08
- FRD: `frd.md`
- HLA: `hla.md`
- Research: `prior-art-research.md`

## Delivery shape

This effort uses one long-lived website PR plus a closeout PR. On 2026-08-08, the operator rejected
merging design artifacts separately from a site that is ready to go live. PR #439 therefore remains
draft and accumulates the reviewed design, selected brand assets, local custom-404 demo,
deterministic main site, CI, and automatic Pages deployment. It merges only as a complete
default-host slice. A small go-live/closeout PR then records production acceptance evidence, final
truthful checkbox flips, and `locked.md` after the implementation merge has deployed and DNS is
live.

`locked.md` never merges before production acceptance. Every earlier merged slice is complete and
operable on its own terms; the custom-domain slice follows only because its external verification
requires the publishing workflow to exist on `main`.

Main-page content integration is gated on the onboarding-and-discovery effort's canonical README
bootstrap source landing on `main`. Brand, 404, and game design and a local-only demo can proceed
while that contract is pending because they consume no onboarding content. They remain unmerged and
not deployed until the complete website is ready. No website code binds to the onboarding feature
branch.

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
- [ ] Complete the checklist's pending Firefox, WebKit, screen-reader, and human touch/motion rows
      on available surfaces before declaring Phase 2 fully accepted.

Definition of done: the brand and 404 game are review-clean and locally demonstrable from PR #439,
but are not deployed or merged as a partial website.

## Phase 3: merged-contract pickup and site LLD

- [ ] Check the website feature directory on `origin/main` for late messages before starting.
- [ ] Confirm onboarding Phase 3 is merged to `main`; inspect its canonical bootstrap source,
      generator, README block markers, tests, and permanent docs at HEAD. If it landed with no clean
      consumer seam, raise the incompatibility rather than parsing a generated harness wrapper.
- [ ] Delegate `site-lld.md` to an `agentworks-dev` subagent. It pins exact source paths and
      extraction errors, generated product paragraphs, site-owned non-claim labels, site files,
      placeholder vocabulary, DOM outline, responsive layout, visual tokens, copy states, builder
      CLI, output tree, project-base to custom-domain-base go-live transition, and test matrix.
- [ ] Lead reviews the LLD against the FRD/HLA and updates the lead-owned artifacts if
      implementation detail exposes an upstream gap.
- [ ] `agentworks-reviewer` reviews the LLD; all valid findings are resolved and re-reviewed.

Definition of done: every implementation seam is pinned to merged code at file and symbol level,
with no branch-only dependency or unresolved visual/content contract.

## Phase 4: deterministic site and content contracts

- [ ] Delegate implementation to an `agentworks-dev` subagent with ownership of `website/`, focused
      website tests, and generated-output ignore entries. The subagent is not alone in the codebase
      and must preserve concurrent changes.
- [ ] Build the one-page source and deterministic standard-library builder per the LLD; generated
      files are written only below an explicit output directory and remain uncommitted. Extend the
      Phase 2 builder seam rather than introducing a second rendering path, and pin the Pages
      project-base to custom-domain-base transition before deployment.
- [ ] Consume the merged canonical bootstrap source and enforce byte identity across source, README,
      and decoded built HTML, including newline and fence-edge cases.
- [ ] Generate every problem/principle passage from uniquely selected permanent-doc paragraphs using
      the closed normalization contract; fail on missing/duplicate headings, paragraph drift, or
      unsupported Markdown. Keep site-owned connective copy free of product claims.
- [ ] Implement semantic document structure, metadata, GitHub/PyPI/rationale/security links, local
      responsive CSS, and the selected AGW visual system.
- [ ] Implement progressive copy behavior with accessible success/failure feedback and a fully
      usable no-JavaScript path.
- [ ] Add deterministic, malformed-input, escaping, no-placeholder, no-external-runtime,
      source-extraction, DOM-contract, and copy-behavior tests.
- [ ] Add `website/README.md` with local build/test instructions, content ownership, output
      contract, Pages/DNS setup, deployment, rollback, and hosting-migration guidance.
- [ ] Update the top-level `README.md` component inventory in the same PR so it no longer claims
      `cli/` is the repository's only component after `website/` lands.
- [ ] Run focused tests and `./scripts/lint-files.sh --fix`; confirm generated output does not
      appear in Git status.
- [ ] `agentworks-reviewer` and a fresh-eyes reviewer inspect the implementation; valid findings go
      back to the implementing subagent and are resolved before re-review.

Definition of done: R1, R2, R4, R5, C1-C3, AC3, and the build-time portion of R3 hold locally with a
review-clean deterministic artifact.

## Phase 5: CI and GitHub Pages deployment

- [ ] Delegate workflow implementation to an `agentworks-dev` subagent with ownership of the Pages
      workflow and the website job in existing CI. It must preserve existing workflow conventions
      and the `ci-success` umbrella.
- [ ] Verify current stable major releases of official checkout, configure-pages,
      upload-pages-artifact, and deploy-pages actions at implementation time and pin them.
- [ ] Add the website build/test job to pull-request CI and to `ci-success` so branch protection
      cannot omit it.
- [ ] Add the Pages workflow: build and upload from a clean checkout, deploy on `main`, least
      permissions, `github-pages` environment, and safe concurrency.
- [ ] Keep the publishing workflow free of path filters so changes to authoritative inputs outside
      `website/` always rebuild and verify the deployed artifact.
- [ ] Ensure pull requests exercise the build without deploying or acquiring Pages write/OIDC
      permissions.
- [ ] Before merge, obtain operator approval to enable GitHub Actions as this repository's Pages
      source and configure the `github-pages` environment so only `main` may deploy; record the
      non-secret setting evidence in `website/README.md` and the PR.
- [ ] Verify the uploaded artifact contains only intended site files and identifies the source
      commit.
- [ ] Run workflow syntax checks, focused site tests, full repository CI-equivalent gates,
      locked-SDD checks, and file-quality lint.
- [ ] `agentworks-reviewer` and a fresh-eyes reviewer inspect permissions, trigger behavior,
      artifact boundaries, and failure modes; valid findings are resolved and re-reviewed.
- [ ] Open the implementation PR ready for review with per-step review evidence and exact gates;
      triage Copilot comments, request the roadmap lead's standalone-effort review, resolve all
      valid findings, and hand the reviewed PR to the operator for merge.
- [ ] After merge, verify the matching commit deploys successfully at the default Pages URL without
      a manual publish step. Leave custom-domain and closeout boxes unchecked.

Definition of done: R3 and AC2 are implemented as a least-privilege, review-clean automatic Pages
pipeline, the implementation is merged and live at the default Pages URL, and custom-domain
activation is ready to begin.

## Phase 6: acceptance, domain activation, and closeout

- [ ] Start the go-live branch from `main`, confirm the implementation commit is deployed at the
      default Pages URL, and open a small closeout PR once evidence and artifacts are ready.
- [ ] Re-inventory GoDaddy `A`, `AAAA`, `CNAME`, `MX`, `TXT`, and `CAA` records immediately before
      cutover; save a non-secret before-state and exact rollback record. If any record's purpose is
      unclear, stop rather than remove it. Ensure any restrictive CAA policy permits
      `letsencrypt.org`.
- [ ] Verify `agentworks.build` at the WayfarerLabs organization level before attaching DNS; retain
      the generated TXT record and record non-secret evidence.
- [ ] Reconfirm and record the deployed default Pages URL before attaching the custom domain.
- [ ] Set `agentworks.build` as this repository's custom domain and enforce HTTPS when GitHub makes
      the option available.
- [ ] With explicit operator approval for the recorded cutover, remove only identified conflicting
      parking/forwarding records; add the current GitHub-documented apex `A` records and `www`
      CNAME. Do not add a wildcard.
- [ ] Verify with DNS queries that apex A, `www` CNAME, and organization verification TXT records
      are correct after propagation; recheck `AAAA`, `MX`, other `TXT`, and `CAA` against the saved
      before-state and intended delta.
- [ ] Verify production HTTPS, certificate hostname, apex canonical metadata, `www` redirect, GitHub
      and PyPI links, no runtime third-party requests, and byte-identical copied bootstrap.
- [ ] Verify an unknown production path returns the semantic custom 404 and keeps its home link
      usable without JavaScript.
- [ ] Verify the selected original twin-plume mark and the hidden game in production: bounded idle
      cue, no preflight instruction text, Space/arrow/vi behavior, tap/hold/drag behavior on a
      narrow touch viewport, independent plume response, safe and unsafe contact, agent entry,
      persistent- per-run NOC power-up, lander departure, exact success status, restart/exit reset,
      no storage or runtime request, background pause, and reduced-motion presentation.
- [ ] Run human acceptance from a clean context: a newcomer understands the product and hands the
      block to an agent in under one minute. Record timing and any unexplained intervention.
- [ ] Run accessibility acceptance: keyboard-only flow, visible focus, landmarks/headings, copy
      status, 320 CSS pixel reflow, 400 percent zoom, contrast evidence, and reduced-motion
      behavior.
- [ ] Verify the implementation merge's site-source commit became live without a manual publish
      step, proving AC2 in production; if a second proof is warranted, merge a separately reviewed
      harmless content change rather than manufacturing a history-only change inside closeout.
- [ ] Record dated acceptance evidence in this feature directory without secrets or account tokens.
- [ ] Promote every load-bearing operating fact to `website/README.md`; no permanent file depends on
      this SDD.
- [ ] Create `locked.md` with the final state and date only after all acceptance criteria are true;
      run final gates, obtain the required closeout review, and hand the ready PR to the operator
      for merge.

Definition of done: AC1-AC8 hold at `https://agentworks.build`, R1-R9 and C1-C4 are production
facts, the deployment is operable without this SDD, all plan boxes are truthful, and the effort is
locked.

## Escalation triggers

- The onboarding canonical bootstrap lands without a source or consumer interface the website can
  use without parsing generated wrappers.
- GitHub Pages policy no longer permits the intended project site, or the site gains a commercial
  transaction/SaaS purpose.
- The repository or organization plan cannot enable Pages or a custom domain.
- GoDaddy contains records whose purpose is unclear and whose removal could affect mail or another
  service.
- GitHub cannot provision a valid apex and `www` certificate after documented propagation windows.
- Meeting the content or visual acceptance criteria requires a framework, backend, external asset,
  analytics, or scope beyond the FRD.
