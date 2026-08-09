# Lunar Deployment Browser Checklist

The full-site demo uses the same builder and game sources:

```bash
python3 website/build.py --repo-root . --output /tmp/agentworks-site-demo --site-base /
python3 -m http.server --directory /tmp/agentworks-site-demo 8000
```

Use this package-free checklist against `/404.html` in the complete built site. Record each
execution so a future reader can distinguish verified behavior from an expectation that has not been
run.

Phase 4B changed the shared header and footer and added `/manifesto/`. Automated source, generated
document, and model checks pass, but no browser execution has been recorded for that source. All
shared-shell rows and any 404 row that names the new breadcrumb route-home action are pending.

## Local demo

Build and serve from the repository root:

```console
python3 website/build.py --repo-root . --output /tmp/agentworks-site-demo --site-base /
python3 -m http.server --directory /tmp/agentworks-site-demo 8000
```

Open `http://localhost:8000/404.html` from the complete linked site. Keep the browser network panel
open with request preservation enabled. Disable the browser cache for request-log checks.

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

Repeat the full checklist in current Chromium and Firefox. Repeat the responsive rows on a touch
browser. Safari or another WebKit browser is required before public launch.

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

- [ ] With JavaScript disabled, the page shows one 404 heading, explanatory text, a working linked
      `Agentworks` breadcrumb home crumb, the lander, lunar surface, landing zone, and dark
      operations center. There is no body-level home link.
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
- [x] Activating the start button by keyboard or ordinary click starts without thrust, reveals the
      single control paragraph, hides the start button, and moves focus to the game without
      scrolling.
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
- [ ] Escape on the active shell exits, cuts thrust, hides controls, restores settled preflight, and
      focuses the start button without scrolling. Escape on a shell link or outside the shell keeps
      browser behavior.
- [x] After success or failure, R restarts with fresh fuel, a dark operations center, a closed bay,
      no agent, and shell focus. R has no mission effect in other states.
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

- [ ] At 320 CSS pixels wide, the page has no horizontal overflow, clipped controls, obscured
      breadcrumb, or start target smaller than the pinned full-silhouette bounds.
- [ ] At 400 percent zoom, all error content, home navigation, controls, focus outlines, scene, and
      status remain reachable without two-dimensional page scrolling.
- [x] In touch landscape, the scene fits its container, the active shell alone suppresses touch
      actions, and browser navigation and zoom remain available outside it.
- [x] On a wide viewport, the scene remains capped at 60 rem with a 25:16 ratio; terrain never
      covers the landing zone, operations center, focus outline, controls, or status.

## Shared shell acceptance

- [ ] Home shows the AGW rocket at two to three times the small-header scale, repository-sourced
      identity, and onboarding availability. It has no small header mark. Home, Manifesto, Security,
      and 404 each show one GitHub and one PyPI icon-and-text link in the header and one Manifesto
      and one Security link in the footer, with no body duplicate.
- [ ] In current Chromium, Firefox, and WebKit at 320 CSS pixels, 400 percent zoom, touch landscape,
      and wide desktop, Home, Manifesto, Security, and 404 have no page overflow, clipped text or
      navigation, overlap, or fixed-height content loss. Record browser versions, viewport, date,
      and result before public release.
- [ ] With JavaScript disabled, Home, Manifesto, and Security retain all content and links, while
      404 retains its message, named scene, and breadcrumb route-home action. No surface depends on
      terminal familiarity.
- [ ] Keyboard-only traversal reaches each visible skip link, breadcrumb home crumb, GitHub, PyPI,
      footer links, and the 404 lander start control in source order with a visible focus outline.
- [ ] A screen reader reports each page title, header, main, footer, one `h1`, nested section
      headings, breadcrumb current state, named navigation, visible CTA labels without decorative
      icon noise, sourced links, and 404 status/focus behavior. Initial game controls remain hidden.
- [ ] With reduced motion enabled, Home, Manifesto, and Security remain motion-free and 404 follows
      the existing no-cue, direct-success contract. Human touch checks confirm page scroll outside
      the active scene and tap, hold, and drag behavior inside it.
- [ ] In a clean context, a visitor identifies what Agentworks is, sees that guided onboarding is
      not yet published, and can choose GitHub, PyPI, Manifesto, or Security without explanation.
      Record timing and any intervention.
