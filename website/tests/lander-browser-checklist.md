# Lunar Deployment Browser Checklist

Use this package-free checklist against the built 404 artifact. Record each execution so a future
reader can distinguish verified behavior from an expectation that has not been run.

## Local demo

Build and serve from the repository root:

```console
python3 website/build.py --only 404 --repo-root . --output /tmp/agentworks-404-demo --site-base /
python3 -m http.server --directory /tmp/agentworks-404-demo 8000
```

Open `http://localhost:8000/404.html`. Keep the browser network panel open with request preservation
enabled. Disable the browser cache for request-log checks.

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

## Static recovery and initial presentation

- [x] With JavaScript disabled, the page shows one 404 heading, explanatory text, a working
      `Return to agentworks.build` link, the lander, lunar surface, landing zone, and dark
      operations center.
- [x] With JavaScript disabled, no start target or control instructions are exposed visually or to
      the accessibility tree. The document has header, main, and footer landmarks in that order.
- [ ] With JavaScript enabled and normal motion, each reload gives one subtle three-pulse plume cue
      lasting 2.4 seconds. It does not replay after Escape, restart, focus changes, or tab hiding.
- [x] With reduced motion enabled before reload, the cue does not run and settled short plumes
      remain visible.
- [ ] Before activation, the scene is announced as a named image. Its description mentions the
      lander, surface, landing zone, and dark network operations center, but gives no controls.
- [x] The transparent start target covers the complete lander silhouette and is at least 44 by 44
      CSS pixels. It has no visible outline until keyboard focus is visible.

## Focus and keyboard

- [x] Tab reaches the home link and named start button in logical document order. No Tab key is
      intercepted and focus is never trapped.
- [x] Activating the start button by keyboard or ordinary click starts without thrust, reveals the
      single control paragraph, hides the start button, and moves focus to the game without
      scrolling.
- [x] Pressing unmodified Space on the document body or scene starts, immediately commands thrust,
      and does not scroll. Holding that same physical Space key continues thrust after focus moves;
      releasing it ends thrust.
- [ ] Modified Space, repeated preflight Space, and Space targeted at the home link or another
      interactive or editable element retain ordinary browser behavior and do not start.
- [x] During flight, Space and Up provide equal thrust. Left and H increase the right plume and turn
      left. Right and L increase the left plume and turn right. Holding both aliases and releasing
      only one leaves the other active.
- [ ] Active control keys suppress browser scrolling only while the game shell is active. Shift does
      not change flight mappings. Control, Alt, and Meta combinations retain browser behavior.
- [ ] Escape on the active shell exits, cuts thrust, hides controls, restores settled preflight, and
      focuses the start button without scrolling. Escape on the home link or outside the shell keeps
      browser behavior.
- [ ] After success or failure, R restarts with fresh fuel, a dark operations center, a closed bay,
      no agent, and shell focus. R has no mission effect in other states.
- [ ] During play the scene SVG and all decorative descendants are silent to a screen reader. The
      shell is announced as `Lunar deployment game`, controls are described once, and status changes
      are polite and restrained.

## Pointer and touch

- [x] The first deliberate lander tap starts without thrust. Scrolling, text selection, zoom, and
      links outside an active game remain normal.
- [ ] During flight, primary pointer down captures that pointer and immediately commands equal
      thrust. A second pointer and non-primary mouse buttons are ignored.
- [x] Holding sustains thrust. Horizontal drag right lengthens the left plume and turns right; drag
      left lengthens the right plume and turns left. Vertical travel does not affect commands.
- [x] A tap released within 180 milliseconds and 10 CSS pixels produces at least a 140-millisecond
      equal-thrust pulse. Another down is ignored until that pulse ends.
- [ ] Pointer up, cancellation, lost capture, window blur, shell focus loss, tab hiding, contact,
      failure, Escape, restart, and a simulated frame stall all release capture and leave no stuck
      thrust. Repeat each teardown once to confirm it is harmless when already clear.

## Flight, collision, and sequence

- [x] An upright, slow touchdown with both feet inside the marked zone freezes safely and announces
      `Touchdown confirmed. Deploying agent.`
- [ ] A fast, tilted, rotating, one-foot-outside, surface-short, operations-center, or out-of-bounds
      contact enters the restrained failure state. Nothing flashes, shakes, explodes, moves the
      page, changes the home link, or emits sound.
- [ ] Failure announces exactly `Landing unsuccessful. Press R to restart or Escape to exit.` and
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
- [ ] Turning reduced motion on during the post-touchdown sequence immediately completes the same
      powered success result. Changing it during flight does not alter physics.
- [ ] Restart, Escape, and reload each clear powered state. No mission state survives a new run or
      reload.

## Lifecycle and request audit

- [ ] Hiding the tab pauses every visible CSS animation, cancels flight frames, releases pointer
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

- [x] At 320 CSS pixels wide, the page has no horizontal overflow, clipped controls, obscured home
      link, or start target smaller than the pinned full-silhouette bounds.
- [x] At 400 percent zoom, all error content, home navigation, controls, focus outlines, scene, and
      status remain reachable without two-dimensional page scrolling.
- [x] In touch landscape, the scene fits its container, the active shell alone suppresses touch
      actions, and browser navigation and zoom remain available outside it.
- [x] On a wide viewport, the scene remains capped at 60 rem with a 25:16 ratio; terrain never
      covers the landing zone, operations center, focus outline, controls, or status.
