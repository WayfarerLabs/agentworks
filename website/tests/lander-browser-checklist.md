# Lunar Deployment Browser Checklist

The full-site demo uses the same builder and game sources:

```bash
python3 website/build.py --repo-root . --output /tmp/agentworks-site-demo --site-base /
python3 -m http.server --directory /tmp/agentworks-site-demo 8000
```

Use this package-free checklist against `/lander/` for ordinary game work and `/404.html` for the
fallback in the complete built site. Record each execution so a future reader can distinguish
verified behavior from an expectation that has not been run.

Phase 4C adds `/lander/`, the final footer icon link, shared native Exit and Restart controls, and a
compact 404 heading. Automated source, generated-document, and model checks pass, but no browser
execution has been recorded for that source. All five-page shared-shell rows and every row that
names the new route, footer link, or native controls are pending.

## Local demo

Build and serve from the repository root:

```console
python3 website/build.py --repo-root . --output /tmp/agentworks-site-demo --site-base /
python3 -m http.server --directory /tmp/agentworks-site-demo 8000
```

Open `http://localhost:8000/lander/` for the normal game, then repeat fallback-specific rows at
`http://localhost:8000/404.html`. Also inspect Home, Manifesto, and Security so the run covers all
five pages. Keep the browser network panel open with request preservation enabled. Disable the
browser cache for request-log checks.

## Execution record

- Date: 2026-08-08
- Browser and version: Chromium 151.0.7922.71, headless DevTools Protocol
- Operating system: Debian GNU/Linux 12, Linux 6.1.0-51-arm64
- Viewport and zoom: 1000 by 900 desktop, 320 by 640 narrow, 1600 by 1000 wide, 800 by 320 touch
  landscape, and 400 percent page scale
- Motion preference: no preference and reduced
- Tester: Codex automated browser acceptance using Chromium DevTools Protocol
- Outcome: PARTIAL PASS, 41 Chromium browser assertions plus the Node and Python contracts passed;
  unchecked and unavailable acceptance rows remain pending
- Notes: The request audit observed only CSS, two modules, SVG, and Chromium's automatic
  `/favicon.ico` probe. Game actions initiated no requests. Firefox, Safari or WebKit, and a screen
  reader are pending because those executables and assistive surfaces are unavailable locally.

### Recorded Chromium evidence

- Desktop and narrow layouts had no horizontal overflow. The start target remained at least 44 CSS
  pixels, the wide scene stayed at 60 rem and 25:16, and recovery remained reachable at 400 percent
  page scale.
- Tab order reached home then start. Space start, native click, Escape recovery, H/L and arrow
  differential thrust, held Space/Up aliases, and immediate blur teardown passed.
- Emulated touch activation, minimum tap pulse, left and right drag, vertical-travel independence,
  cancellation teardown, landscape fit, and active-shell-only touch suppression passed.
- Reduced motion suppressed the cue while computed plume transforms still changed with thrust. A
  safe reduced-motion touchdown atomically produced powered success.
- A normal safe raw-pose contact reached landed, bay/agent route, monotonic power stages, departure,
  static antenna arcs, and exact success. R restored full fuel and dark per-run state. Unsafe
  contact produced the exact failure status.
- Cookies, web storage, databases, and service workers were empty. Destroy and script-disabled
  reload both retained the semantic 404, visible scene, and working home recovery.

### Pending acceptance

- Firefox execution: pending, no Firefox executable is installed.
- Screen-reader announcements: pending, no screen-reader or accessibility-driver surface is
  installed.
- Safari or WebKit execution: pending until go-live on a host with that browser engine.
- Human visual review of motion quality and touch hardware feel: pending; CDP verified state,
  geometry, computed styles, event behavior, and timing contracts.

## Operator pre-merge acceptance

- Date: 2026-08-10
- Source viewed: `3bad34ab18194fe5a0acb54d187863dbed5ba8ad`
- Browsers: current Google Chrome and Microsoft Edge on the operator's port-forward machine; exact
  browser build strings were not supplied
- Tester: operator
- Outcome: PASS; the operator confirmed that the site is fine in both required pre-merge browsers
- Final requested delta: `a6f6522d` only reduces the TOC list font to `0.92rem`, bolds the complete
  `SSH-over-Tailscale control plane` phrase from its canonical README source, and removes three
  behavior-neutral validation residues. The required computed-layout suite, complete website and
  game suites, and both deterministic-base builds pass on that delta. Firefox/WebKit, spoken
  screen-reader, physical mobile/touch, and additional-device acceptance remain post-launch work.

Before merge, repeat the applicable preview checklist in current Chrome and Edge, the two browsers
available to the operator on the machine with port-forward access. After the site is public, repeat
the full engine and responsive rows in Firefox and Safari/WebKit and on physical touch hardware.
Those post-launch findings feed the next website work round and remain required before final
production closeout.

## Phase 4C permanent acceptance

Record a new dated run against the exact implementation SHA. Chrome and Edge own the operator's
pre-merge functional gate. Firefox and WebKit remain post-launch independent-engine gates. Spoken
screen-reader quality and physical touch/motion feel require humans on those actual surfaces after
the public host is reachable. Emulation and an accessibility-tree dump are useful evidence, but do
not close those rows or the final production-acceptance record.

- [x] In current Chrome and Edge before merge, exercise `/lander/` and `/404.html` with JavaScript
      enabled and disabled, at normal and reduced motion, and at every responsive viewport below.
      Record the exact browser version and source SHA when available; otherwise record the explicit
      evidence limitation with the operator's disposition.
- [ ] After launch, in Firefox, repeat the complete keyboard, pointer, lifecycle, reflow,
      computed-style, and reduced-motion pass. Record the exact browser version and source SHA.
- [ ] After launch, in Safari or another WebKit browser, repeat the same engine pass.
- [ ] After launch, with a spoken screen reader, verify initial scene naming, hidden preflight
      controls, the application transition, concise control description, polite status changes,
      native Exit and Restart names/states, and focus destinations. Record screen reader, browser,
      and versions.
- [ ] After launch, on physical touch hardware, verify start, tap impulse, hold, drag, Exit,
      Restart, scroll outside the active scene, and human motion quality. Record device, operating
      system, browser, and input observations.
- [ ] After launch, on at least one additional computer or device outside the pre-merge machine,
      smoke Home, Manifesto, Security, Lander, and 404 over the public host. Record device,
      operating system, browser, viewport, navigation, layout, and game observations; route defects
      into the next website work round.
- [ ] On all five pages, inspect every reviewed shell link with computed styles. Each link is
      visible, intersects the viewport, has nonzero bounds, receives keyboard focus with the full
      outline, and is reachable by pointer without overlap. Temporarily apply an offscreen
      `position: absolute; left: -10000px` canary to a copied link and prove this browser check
      fails even though the static declaration checks do not claim to detect it.
- [ ] Measure the Home hero's actual computed bounds and the compact header mark's computed bounds.
      Record both, while evaluating the accepted `3.2rem` to `4.8rem` hero width against the
      historical pre-refinement `1.6rem` header baseline. Do not reinterpret the requirement as two
      to three times the current `1.2rem` compact mark.

## Interim shell execution record

- Date: 2026-08-09
- Source: `1798ca9a9669588fe72eef2cbfbf80a0faac7226`
- Browser and version: Chromium 151.0.7922.71, headless DevTools Protocol 1.3
- Operating system: Debian GNU/Linux 12, Linux 6.1.0-51-arm64
- Viewport and zoom: 320 by 640 narrow, 800 by 320 emulated-touch landscape, 1600 by 1000 wide, and
  320 by 900 reflow equivalent to a 1280-pixel viewport at 400 percent
- Motion preference: no preference, reduced before load, and reduced changed during flight and
  deployment
- Tester: isolated `agentworks-tester` acceptance using Chromium DevTools Protocol
- Outcome: HISTORICAL PARTIAL PASS, 113 of 113 measured assertions passed with no product defect at
  the recorded source; cross-engine, spoken screen-reader, and physical touch/motion acceptance
  remained pending
- Clean context: DOM ready in 4.7 milliseconds and load in 8.9 milliseconds; the tester identified
  the product, interim onboarding status, repository, package, rationale, and security choices from
  rendered copy with no intervention
- Cleanup: the localhost server and Chromium process were stopped, their ports were closed, all
  temporary artifacts were removed, and the repository remained clean

The run covered cue timing and replay boundaries; modified and repeated keys; every input teardown;
safe, failed, deployed, reduced-motion, restart, and exit paths; actual tab lifecycle; accessibility
tree and live-region contracts; no-JavaScript recovery; shared-shell focus and layout; request and
storage audits; and all listed Chromium viewport shapes. Headless Chromium has no browser-UI zoom
control, so the 400-percent case used the standard 1280 divided by 4 equals 320 CSS-pixel reflow
equivalent. Chromium's accessibility tree passed, but it is not evidence of spoken announcement
quality. Emulated touch geometry, events, and timing passed, but it is not a physical-device feel
review.

The later compact-landing refinement materially changed home structure, link placement, and logo
scale. The record above remains evidence for its named source and for unchanged 404 behavior, but it
does not accept the current home page. Every shared-shell row that exercises home is pending until a
new run records the refined source.

## Static recovery and initial presentation

- [ ] With JavaScript disabled, `/lander/` shows one `Lunar deployment` heading and the complete
      static scene. `/404.html` shows one `Page not found` heading, explanatory text, a working
      linked `Agentworks` breadcrumb home crumb, and the same scene. There is no error-code eyebrow
      or body-level home link.
- [x] With JavaScript disabled, no start target or control instructions are exposed visually or to
      the accessibility tree. The document has header, main, and footer landmarks in that order.
- [x] With JavaScript enabled and normal motion, each reload gives one subtle three-pulse plume cue
      lasting 2.4 seconds. It does not replay after Escape, restart, focus changes, or tab hiding.
- [x] With reduced motion enabled before reload, the cue does not run and settled short plumes
      remain visible.
- [ ] Before activation, the scene is announced as a named image. Its description mentions the
      lander, surface, landing zone, and dark network operations center, but gives no controls.
- [x] The transparent start target covers the complete lander silhouette and is at least 44 by 44
      CSS pixels. It has no visible outline until keyboard focus is visible.

## Focus and keyboard

- [ ] Tab reaches the breadcrumb home crumb, GitHub, PyPI, footer links, and named start button in
      logical document order. No Tab key is intercepted and focus is never trapped.
- [ ] Activating the start button by keyboard or ordinary click starts without thrust, reveals the
      single control paragraph and native `Exit mission` button, keeps `Restart mission` hidden,
      hides the start button, and moves focus to the game without scrolling.
- [x] Pressing unmodified Space on the document body or scene starts, immediately commands thrust,
      and does not scroll. Holding that same physical Space key continues thrust after focus moves;
      releasing it ends thrust.
- [ ] Modified Space, repeated preflight Space, and Space targeted at the breadcrumb home crumb or
      another interactive or editable element retain ordinary browser behavior and do not start.
- [x] During flight, Space and Up provide equal thrust. Left and H increase the right plume and turn
      left. Right and L increase the left plume and turn right. Holding both aliases and releasing
      only one leaves the other active.
- [x] Active control keys suppress browser scrolling only while the game shell is active. Shift does
      not change flight mappings. Control, Alt, and Meta combinations retain browser behavior.
- [ ] Escape and native `Exit mission` on the active shell call the same exit operation: cut thrust,
      hide controls and actions, restore settled preflight, and focus the start button without
      scrolling. Escape on a shell link or outside the shell keeps browser behavior.
- [ ] After success or failure, both R and native `Restart mission` start the same fresh run with
      full fuel, a dark operations center, a closed bay, no agent, and shell focus. Restart is
      hidden and disabled in every non-terminal state.
- [ ] During play the scene SVG and all decorative descendants are silent to a screen reader. The
      shell is announced as `Lunar deployment game`, controls are described once, and status changes
      are polite and restrained.

## Pointer and touch

- [x] The first deliberate lander tap starts without thrust. Scrolling, text selection, zoom, and
      links outside an active game remain normal.
- [x] During flight, primary pointer down captures that pointer and immediately commands equal
      thrust. A second pointer and non-primary mouse buttons are ignored.
- [x] Holding sustains thrust. Horizontal drag right lengthens the left plume and turns right; drag
      left lengthens the right plume and turns left. Vertical travel does not affect commands.
- [x] A tap released within 180 milliseconds and 10 CSS pixels produces at least a 140-millisecond
      equal-thrust pulse. Another down is ignored until that pulse ends.
- [x] Pointer up, cancellation, lost capture, window blur, shell focus loss, tab hiding, contact,
      failure, Escape, restart, and a simulated frame stall all release capture and leave no stuck
      thrust. Repeat each teardown once to confirm it is harmless when already clear.
- [ ] Touch activation of native Exit and terminal-state Restart follows the same controller,
      teardown, model-event, and focus lifecycle as Escape and R. Neither action needs a keyboard.

## Flight, collision, and sequence

- [x] An upright, slow touchdown with both feet inside the marked zone freezes safely and announces
      `Touchdown confirmed. Deploying agent.`
- [x] A fast, tilted, rotating, one-foot-outside, surface-short, operations-center, or out-of-bounds
      contact enters the restrained failure state. Nothing flashes, shakes, explodes, moves the
      page, changes the home link, or emits sound.
- [x] Failure announces exactly `Landing unsuccessful. Press R to restart or Escape to exit.` and
      accepts both recovery paths.
- [x] After safe touchdown at normal motion, the G bay opens, the terminal-shaped agent descends,
      crosses the surface, and enters the west operations-center door.
- [x] Power proceeds monotonically through west window, server bars and status lights, east window,
      and two antenna arcs. The powered appearance remains through departure and success.
- [x] The lander departs with equal plumes, is clipped above the scene, and produces exactly one
      final live-region update: `Agent deployed. Mission continues.`
- [x] With reduced motion enabled before touchdown, safe contact immediately shows the fully powered
      operations center and final status with no bay, agent route, sequential power, or departure
      motion. Physics remains playable.
- [x] Turning reduced motion on during the post-touchdown sequence immediately completes the same
      powered success result. Changing it during flight does not alter physics.
- [x] Restart, Escape, and reload each clear powered state. No mission state survives a new run or
      reload.

## Lifecycle and request audit

- [x] Hiding the tab pauses every visible CSS animation, cancels flight frames, releases pointer
      capture, and clears held input. Returning to the tab does not jump or simulate hidden time;
      the first visible frame only resets timing.
- [x] Losing shell focus clears input without pausing or changing mission state. Returning focus
      does not restore previously held commands.
- [x] Label the initial same-origin document, module, CSS, and SVG requests in the network log.
      After those loads, starting, flying, failing, restarting, exiting, succeeding, changing
      motion, and hiding or restoring the tab initiate zero additional requests.
- [x] The browser storage panels show no cookies, local or session storage, database, cache storage,
      or service worker created by the page.

## Responsive acceptance

- [x] At 320 CSS pixels wide, the page has no horizontal overflow, clipped controls, obscured
      breadcrumb, or start target smaller than the pinned full-silhouette bounds.
- [x] At 400 percent zoom, all detail headings, 404 content, home navigation, native actions, focus
      outlines, scene, and status remain reachable without two-dimensional page scrolling.
- [x] In touch landscape, the scene fits its container, the active shell alone suppresses touch
      actions, and browser navigation and zoom remain available outside it.
- [x] On a wide viewport, the scene remains capped at 60 rem with a 25:16 ratio; terrain never
      covers the landing zone, operations center, focus outline, controls, or status.

## Shared shell acceptance

- [x] Home shows the AGW rocket at the accepted historical-baseline size, repository-sourced
      identity, and onboarding availability. It has no small header mark. Home, Manifesto, Security,
      Lander, and 404 each show one GitHub and one PyPI icon-and-text link in the header and exactly
      three footer destinations: Manifesto, Security, then the icon-only Lander link. No destination
      is duplicated in the body.
- [x] In Chrome and Edge before merge, verify at 320 CSS pixels, 400 percent zoom, touch landscape,
      and wide desktop that Home, Manifesto, Security, Lander, and 404 have no page overflow,
      clipped text or navigation, overlap, or fixed-height content loss. Record browser versions,
      viewport, date, and result when available, or record the explicit evidence limitation.
- [ ] After launch, repeat that complete shared-shell matrix in Firefox and WebKit and record the
      browser versions, viewport, date, and result.
- [x] With JavaScript disabled, Home, Manifesto, and Security retain all content and links, Lander
      retains its heading and named scene, and 404 retains its message, named scene, and breadcrumb
      route-home action. No surface depends on terminal familiarity.
- [ ] Keyboard-only traversal reaches each visible skip link, breadcrumb home crumb, GitHub, PyPI,
      all three footer links, and both Lander/404 start controls in source order with a visible
      focus outline. The footer rocket target is at least 24 by 24 CSS pixels and is not clipped.
- [ ] A screen reader reports each page title, header, main, footer, one `h1`, nested section
      headings, breadcrumb current state, named navigation, visible CTA labels without decorative
      icon noise, sourced links, and 404 status/focus behavior. Initial game controls remain hidden.
- [ ] With reduced motion enabled, Home, Manifesto, and Security remain motion-free and 404 follows
      the existing no-cue, direct-success contract. Human touch checks confirm page scroll outside
      the active scene and tap, hold, and drag behavior inside it.
- [x] In a clean context, a visitor identifies what Agentworks is, sees that guided onboarding is
      not yet published, and can choose GitHub, PyPI, Manifesto, or Security without explanation.
      Record timing and any intervention.
