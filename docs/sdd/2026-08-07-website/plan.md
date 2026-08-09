# Plan: The agentworks.build Website

- Status: Staged interim release approved; implementation in progress
- Date: 2026-08-07
- Last revised: 2026-08-09
- FRD: `frd.md`
- HLA: `hla.md`
- Research: `prior-art-research.md`

## Delivery shape

This effort now uses two complete website releases, a small interim acceptance-evidence PR, and a
final closeout PR. The operator's 2026-08-08 direction still governs incomplete design/demo work:
the existing website PR (number 439) remains draft and does not merge merely to publish artifacts or
the game. The 2026-08-09 direction defines a new complete first release: that PR may become ready
and merge when the useful interim home and security pages, selected brand, accepted custom 404, CI,
automatic Pages deployment, runbook, and default-host acceptance are all complete. The custom-domain
activation then publishes that honest interim release at `agentworks.build`, and the small evidence
PR records the observed result.

After onboarding Phase 3 lands on `main`, a separately reviewed integration PR replaces the bounded
availability notice with the canonical README bootstrap and copy enhancement. It does not redesign
the page, fork the content, or create another deployment path. A final closeout PR records complete
production acceptance, final truthful checkbox flips, and `locked.md`.

`locked.md` never merges before AC1-AC14 and the final production requirements are accepted. Every
earlier merge is independently useful and operable: the interim release does not claim onboarding
exists, and the onboarding release consumes only the eventual canonical contract from `main`. No
website code binds to the onboarding feature branch.

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
- 2026-08-09: as a narrow expansion from the original one-page scope, the operator adds a visually
  secondary `We take security seriously.` path to a dedicated security deep dive; the home page
  stays concise and non-preachy. The operator also requires the eventual onboarding disclosure to
  identify the intended workstation, the agent's necessary full local file/command access, and the
  recommendation for a strict, observable harness posture. Across the site, terminal/TUI cues should
  communicate `simple but powerful` without turning the site into a fake terminal.

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
- [ ] Complete the Phase 2 Firefox, WebKit, screen-reader, and human touch/motion checks before
      proposing a public release.
- [x] Run focused tests and `./scripts/lint-files.sh --fix`; confirm generated output does not
      appear in Git status.
- [x] `agentworks-reviewer` and a fresh-eyes reviewer inspect the implementation; valid findings go
      back to the implementing subagent and are resolved before re-review.

Definition of done: R4-R11, R13, C1-C5, and the local portions of AC5-AC11/AC13-AC14 hold in a
review-clean, deterministic interim artifact that is ready to publish but makes no onboarding claim.

## Phase 5: CI and default Pages deployment

- [ ] Delegate workflow implementation to an `agentworks-dev` subagent with ownership of the Pages
      workflow and the website job in existing CI. It must preserve existing workflow conventions
      and the `ci-success` umbrella.
- [ ] Verify current stable major releases of official checkout, configure-pages,
      upload-pages-artifact, and deploy-pages actions at implementation time and pin them.
- [ ] Add the website build/test job to pull-request CI and to `ci-success` so branch protection
      cannot omit it.
- [ ] Add the Pages workflow: build and upload from a clean checkout, deploy on `main`, least
      permissions, `github-pages` environment, and safe concurrency.
- [ ] Keep the publishing workflow free of path filters so authoritative inputs outside `website/`
      always rebuild and verify the deployed artifact.
- [ ] Ensure pull requests exercise the build without deploying or acquiring Pages write/OIDC
      permissions; verify the uploaded artifact contains only intended files and identifies the
      source commit.
- [ ] Before merge, obtain operator approval to enable GitHub Actions as this repository's Pages
      source and restrict the `github-pages` environment to `main`; record the non-secret setting
      evidence in `website/README.md` and PR #439.
- [ ] Run workflow syntax checks, focused site tests, full repository CI-equivalent gates,
      locked-SDD checks, and file-quality lint.
- [ ] `agentworks-reviewer` and a fresh-eyes reviewer inspect permissions, triggers, artifact
      boundaries, and failure modes; resolve valid findings and re-review.
- [ ] Mark PR #439 ready only when the complete interim release is review-clean. Triage Copilot
      comments, request the saga lead's standalone-effort review, resolve valid findings, and hand
      the reviewed PR to the operator for merge.
- [ ] After merge, verify the matching commit deploys automatically at the default Pages URL. Leave
      custom-domain, onboarding, and lock boxes unchecked.

Definition of done: R3 and AC2 are implemented as a least-privilege automatic pipeline, and the
interim artifact is merged and live at the default Pages URL without a manual publish step.

## Phase 6: interim domain activation and acceptance

- [ ] Start the interim-acceptance branch from `main`, confirm the implementation commit is deployed
      at the default Pages URL, and keep the branch limited to evidence and truthful documentation
      updates while the external activation proceeds.
- [ ] Re-inventory GoDaddy `A`, `AAAA`, `CNAME`, `MX`, `TXT`, and `CAA` records immediately before
      cutover; save the non-secret before-state and exact rollback record in the feature evidence,
      commit it, push it, and open the draft evidence PR before any mutation. If any record's
      purpose is unclear, stop rather than remove it. Ensure any restrictive CAA policy permits
      `letsencrypt.org`.
- [ ] Verify `agentworks.build` at the WayfarerLabs organization level before attaching DNS; retain
      the generated TXT record and record non-secret evidence.
- [ ] Reconfirm the deployed default Pages URL, set `agentworks.build` as this repository's custom
      domain, and enforce HTTPS when GitHub makes the option available.
- [ ] With explicit operator approval for the recorded cutover, remove only identified conflicting
      parking/forwarding records; add the then-current GitHub-documented apex `A` records and `www`
      CNAME. Do not add a wildcard.
- [ ] Verify DNS answers, certificate hostname, HTTPS, apex canonical metadata, `www` redirect, and
      the saved record inventory plus approved delta after propagation.
- [ ] Verify the production home page has sourced product content, permanent links, the semantic
      availability notice, and no bootstrap, install instruction, copy affordance, external runtime
      request, analytics, storage, or unexpanded token.
- [ ] Verify the secondary home-page security link and stable deep-dive URL, sourced threat,
      boundary, and limitation content, private reporting path, no-JavaScript behavior, and
      non-preachy hierarchy.
- [ ] Verify the production custom 404, route-home fallback, original twin-plume mark, hidden game,
      keyboard/vi and touch controls, independent plume response, deployment sequence, exact success
      status, reset boundaries, lifecycle pause, and reduced-motion presentation.
- [ ] Run interim accessibility acceptance: keyboard-only flow, visible focus, landmarks/headings,
      320 CSS pixel reflow, 400 percent zoom, contrast evidence, reduced motion, and recognizable
      controls independent of terminal familiarity.
- [ ] Run clean-context interim acceptance: a newcomer understands what Agentworks is, recognizes
      that guided onboarding is not yet published, and can choose the repository, package,
      rationale, or security path without explanation. Record timing and any intervention.
- [ ] Record dated, non-secret interim acceptance evidence and all load-bearing operating facts in
      `website/README.md` and this feature directory on the existing evidence branch; mark its draft
      PR ready, obtain review, and merge it. Do not create `locked.md` or claim AC3/AC4.

Definition of done: AC1, AC2, AC5-AC11, and AC13-AC14's interim conditions hold at
`https://agentworks.build`; the public site is useful and honest while onboarding remains pending.

## Phase 7: merged onboarding pickup and integration LLD

- [ ] Check the website feature directory on `origin/main` for late messages before starting.
- [ ] Confirm onboarding Phase 3 is merged to `main`; inspect its canonical bootstrap source,
      generator, README block markers, tests, and permanent docs at HEAD. If it has no clean
      consumer seam, raise the incompatibility rather than parsing a generated harness wrapper.
- [ ] Confirm the merged canonical disclosure explicitly states intended-workstation placement, full
      file inspection and command execution access as the workstation account without implicit root,
      separately explicit elevation, and a concrete strict-posture recommendation. If not, stop and
      coordinate with the onboarding owner rather than adding website-owned wording.
- [ ] Delegate `onboarding-integration-lld.md` to an `agentworks-dev` subagent. It pins exact source
      paths, README fence semantics, byte/newline rules, builder inputs and errors, notice removal,
      access-disclosure semantics, bootstrap DOM, copy states, and the complete-release test matrix.
- [ ] Lead reviews the LLD against R1/R2, C1/C3/C5, D3/D6/D10, and the merged onboarding contract.
- [ ] `agentworks-reviewer` reviews the LLD; all valid findings are resolved and re-reviewed.

Definition of done: the final content seam is pinned at file and symbol level against merged code,
with no branch-only dependency or duplicated bootstrap.

## Phase 8: canonical onboarding integration

- [ ] Delegate implementation to an `agentworks-dev` subagent with ownership of the main-page
      onboarding integration, copy enhancement, focused tests, and permanent documentation. The
      subagent is not alone in the codebase and must preserve concurrent changes.
- [ ] Delete the interim notice and make the canonical bootstrap a required builder input; enforce
      byte identity across source, README, and decoded built HTML, including newline and fence-edge
      cases.
- [ ] Add the real semantic `pre`/`code` region and progressive copy behavior with accessible
      success/failure feedback and a fully usable no-JavaScript path. Do not change the established
      URLs, information architecture, visual system, 404, hosting, or DNS.
- [ ] Invert interim guards: forbid the availability notice and require canonical bootstrap markup,
      identity, access-disclosure semantics, copy behavior, and absence of unsupported installation
      or security prose.
- [ ] Run focused tests, full CI-equivalent gates, locked-SDD checks, and file-quality lint.
- [ ] `agentworks-reviewer` and a fresh-eyes reviewer inspect content identity, accessibility,
      migration cleanliness, and scope; resolve valid findings and re-review.
- [ ] Open the integration PR ready for review, triage Copilot comments, request the saga lead's
      review, resolve all valid findings, and hand the reviewed PR to the operator for merge.
- [ ] Verify the matching commit deploys automatically to production and that the interim notice is
      absent.

Definition of done: R1-R3, R12, AC2, AC3, and AC12 hold in production through the existing site and
pipeline, with the temporary notice removed rather than retained as configuration.

## Phase 9: complete acceptance and closeout

- [ ] Start the closeout branch from `main` and verify the onboarding integration commit is the
      production deployment at the apex.
- [ ] Verify production HTTPS, certificate, apex metadata, `www` redirect, permanent links, no
      runtime third-party requests, and byte-identical copied bootstrap.
- [ ] Run human acceptance from a clean context: a newcomer understands the product and hands the
      block to an agent in under one minute. Record timing and any unexplained intervention.
- [ ] Run complete accessibility acceptance: keyboard-only flow, visible focus, landmarks/headings,
      copy status, 320 CSS pixel reflow, 400 percent zoom, contrast evidence, reduced motion, screen
      reader, and touch behavior.
- [ ] Reverify the production custom 404/game contract and confirm the onboarding change did not
      disturb it.
- [ ] Reverify the home/security information architecture, non-preachy security path, sourced
      security claims, and shared terminal/TUI-derived visual system after onboarding integration.
- [ ] Record dated acceptance evidence without secrets or account tokens and promote every
      load-bearing operating fact to `website/README.md`; no permanent file depends on this SDD.
- [ ] Create `locked.md` with the final state and date only after all acceptance criteria are true;
      run final gates, obtain the required closeout review, and hand the ready PR to the operator
      for merge.

Definition of done: AC1-AC14 hold at `https://agentworks.build`, R1-R13 and C1-C5 are production
facts, the deployment is operable without this SDD, all plan boxes are truthful, and the effort is
locked.

## Escalation triggers

- The onboarding canonical bootstrap lands without a source or consumer interface the website can
  use without parsing generated wrappers.
- The onboarding canonical disclosure omits intended-workstation placement, full workstation-account
  file/command access without implicit root, separately explicit elevation, or strict-posture
  requirements, or exposes no stable way to verify them.
- GitHub Pages policy no longer permits the intended project site, or the site gains a commercial
  transaction/SaaS purpose.
- The repository or organization plan cannot enable Pages or a custom domain.
- GoDaddy contains records whose purpose is unclear and whose removal could affect mail or another
  service.
- GitHub cannot provision a valid apex and `www` certificate after documented propagation windows.
- Meeting the content or visual acceptance criteria requires a framework, backend, external asset,
  analytics, or scope beyond the FRD.
