# Lunar Deployment Browser Checklist

<!-- cspell:ignore underframe viewports -->

The full-site demo uses the same builder and game sources:

```bash
python3 website/build.py --repo-root . --output /tmp/agentworks-site-demo --site-base /
python3 -m http.server --directory /tmp/agentworks-site-demo 8000
```

Use this package-free checklist against `/lander/` for ordinary game work and `/404.html` for the
fallback in the complete built site. Record each execution so a future reader can distinguish
verified behavior from an expectation that has not been run.

The continuous expedition materially replaces Phase 4C's one-shot mission. Its terrain, sites,
direct predicted fuel awards, bidirectional cue and camera, parallax sky, checkpoint restart, vacuum
crash, rolling retention, arcade fuel gauge, and installed-agent payoff require a new browser
record. Older checked rows below remain historical evidence for their named source only; they do not
accept the current game.

The Phase 4L automated Chromium witness exercises the 320-by-640 layout and the 320-by-900 CSS-pixel
reflow equivalent of a 1280-pixel viewport at 400 percent. It checks the two single-rect,
non-wrapping controls lines, rail and page overflow, native Retry click and in-shell R activation,
focus-without-scroll ordering, two consecutive exact checkpoint restores, and the pre-checkpoint
initial restore. Run `python3 website/tests/test_lander_phase4l_browser.py`. This headless Chromium
evidence is not a manual Chrome or Edge signoff; the corresponding manual rows remain unchecked.

The Phase 4M automated Chromium witness exercises horizontal travel across both former world-edge
coordinates, pass/reverse/return cue direction, negative and positive camera projection, exact
24-percent sky parallax, bounded two-path sky reconstruction, the opening 15/30 half gauge, and an
uncapped post-award full reference. Run
`python3 -m unittest discover -s website/tests -p 'test_lander_phase4m_browser.py'`. It is automated
projection evidence, not the qualitative or cross-engine signoff below.

The Phase 4Q Chromium witness runs exact `320 by 780`, `320 by 240` 400-percent-equivalent,
`667 by 320` touch-landscape, and `1000 by 780` viewports. It observes actual preflight; seed 11,
seed 41, and static summit/valley windows; high-altitude and bidirectional flight; normal-motion
service; crash; Retry; Exit; footer focus; wheel; and touch transitions. Every stage records width
and height client/scroll metrics for the document, body, header, main, game, shell, stage, rail,
footer, footer navigation, and every header/footer/action descendant. It requires exact viewport and
document equality, zero scroll, fixed stage dimensions and `25:16` ratio, zero world Y translation,
in-viewport descendants, and 44-pixel native actions. The witness also emits 12 distinct PNG
artifacts across the three terrain seeds and four viewports without asserting authored prose or
golden pixels. Inside Chromium it also services 100 real powered sites for each of seeds 11, 39, 41,
and STATIC after a separate finite 12-site production-identical warmup for that seed. The measured
100-site runs time generation plus direct allowance arithmetic and changed-state renders while
enforcing the retention and world-DOM ceilings. Exact listener inventories and stabilized DOM counts
must remain unchanged across all four runs. Run
`python3 -m unittest discover -s website/tests -p 'test_lander_phase4q_browser.py'`. This is
automated Chromium evidence; it does not replace the qualitative terrain or cross-engine acceptance
below.

The Phase 4U witness uses the successor 16/512 m straight-polyline corpus and the
terrain-independent six-candidate site order. It checks visible sharp reversals, normalized `.6`
terrain away from pads, accepted decks at or below `.5`, native support feet, and candidate
rejection without terrain mutation. The geometry witness independently covers 512 profile/order
assignments and 250 geometry classes, while test-owned non-exhaustive flight examples cover all 16
openings plus the closest, farthest, maximum-rise, and maximum-fall representatives. Runtime
allowance is the direct constant-time `quantumCeil(22 + max(0, deckDelta) / 3)` prediction; no route
key, search, simulation, replay, or catalog is present. Capture seed 11, 39, 41, and static evidence
at `1000x780`, `320x780`, true `320x240`, and a true mobile/touch `667x320`; require inner, client,
and scroll width/height equality with zero scroll before and after lifecycle. Screenshots are
qualitative artifacts for operator inspection and are not golden-policed.

The Phase 4S witness supersedes the Phase 4R terrain corpus with global 16/512 m asymmetric
straight-polyline superblock profiles, the visible physical world termini, concrete-contact-only
failure, and the terminal site-4095 service lifecycle. It verifies that former ceiling and
excessive-speed vectors remain ballistic, empty fuel removes thrust without creating failure,
retained-window edges never collide, and large finite sweeps stream global procedural terrain
without giant retained arrays. The route witness covers all 243 exact keys and 320 concrete
assignments, including 205 bootstrap selections over 13 distinct schedules and 38 bounded syntheses,
with the sufficient 12.55 base allowance. The 12 viewport/seed screenshots remain qualitative
artifacts for operator inspection; no test approves wording or golden pixels.

The earlier Phase 4R witness added the shipped seed corpus for the global 16/128 m straight-polyline
terrain, all eight opening profiles, closed-footprint 2.5 m deck clearance, independently
native-footed supports, the 100-key proof catalog, sufficient climb allowance, and unchanged
fixed-height metrics at the same four viewports. Its screenshots are qualitative artifacts for
operator inspection; no test approves wording or golden pixels.

## Arcade presentation acceptance

- [ ] Fly representative seeds 11, 39, 41, and the static seed. Verify visibly straight angular
      facets, broad high/low profiles, substantial use of the normalized 0.1-0.6 band, and no
      curves, rounded terrain joins, shelves, seams, or vertical terrain artifacts.
- [ ] Confirm every deck is visibly exactly 2.5 m above its complete local native footprint and all
      three open lattice columns are reasonably sized, independently meet native terrain, and never
      expose a backing face or floating foot.
- [ ] During low-valley flight, summit flight, ceiling flight, crash, service, Retry, reversal, and
      Exit, confirm the scene never follows vertically, changes height, grows the page, or scrolls.

- [ ] At normal and 400-percent-equivalent reflow, verify the game is a 25:16 scene stage followed
      by a separate normal-flow controls rail. Terrain, fuel, outcomes, and actions stay inside the
      stage, except for the persistent Exit after the rail's controls prose. Nothing overlaps or
      creates horizontal page overflow at 320 CSS pixels.
- [ ] Confirm the left fuel gauge has a graphite boundary, dark track, and one bright bottom-origin
      level. Its color changes independently from its height: orange-red at or below 20 percent,
      amber above 20 through 50 percent, and mint above 50 percent. No numeric fuel value is
      visible. Inspect accessibility output and confirm separate hidden fuel label and value spans
      occur as ordered scene-description references, with no meter, progress, or output role.
- [ ] Service a site with normal motion. The fuel award commits once, then one 20 by 22 pixel
      graphite-and-orange can moves in a straight line from the site's screen position to the gauge
      over exactly 300 milliseconds while the gauge fills linearly. Resize during transfer and
      confirm the can is projected again without restarting, jumping progress, or changing fuel.
- [ ] Repeat service with reduced motion enabled before contact, and enable reduced motion during
      transfer. Both paths complete atomically with no moving can and the same fuel, checkpoint,
      power, and installed-agent result. Hide and restore the tab during transfer and confirm hidden
      time advances neither the can nor the gauge.
- [ ] After service begins, confirm the entering agent becomes one installed glyph inside the NOC
      doorway and remains visible through every NOC power stage, departure, rolling-window
      reconciliation, and checkpoint restart. Exit restores the exact empty doorway and a fresh run
      has no installed glyph. The existing site group gains no descendant.
- [ ] Confirm the reviewed deployment and crash copy use centered bordered in-stage panels with
      exactly one polite live status. Retry follows that status and appears only after failure. A
      persistent Exit follows the controls prose in the final normal-flow rail whenever the shell is
      active. Both native actions remain at least 44 CSS pixels, have visible second-line `r` and
      `<esc>` hints excluded from their accessible names, and expose the matching keyboard
      shortcuts. Focus returns to the scene after Retry and to Start after Exit.
- [ ] Activate every native action by pointer and touch. Its event must not start, steer, capture,
      or pulse flight input; each action fires once through its native click. Repeat with
      interactive and editable descendants placed inside the active stage.
- [ ] With the network panel open, confirm the arcade presentation loads no font, image, or other
      asset and makes no request after the existing same-origin document, stylesheet, modules, and
      SVG loads. The visible arcade text uses the local system monospace stack.

## Continuous expedition acceptance

- [ ] Record the exact source SHA and current Chrome and Edge versions. Exercise both `/lander/` and
      `/404.html`; confirm their `#lander-game` subtrees are byte-equivalent.
- [ ] Fly and safely service both three-site terrain families at different seeded phases. Each
      ordinary leg shows its summit, seeded valley, and next rise. Each target has one elevated H
      platform exactly three lander widths long, one gas can, and one solid NOC. Confirm native
      terrain remains visible beneath it, with one continuous twelve-bay Warren truss and exactly
      three one-metre lattice columns terminating at six independent native-terrain feet, with no
      internal closure stroke, vertical artifact, filled backing face, or pale artifact. The can
      disappears once, fuel increases once, the agent enters, the four battery bars fill
      red/orange/pale-yellow/mint from bottom to top, the three signal arches power in order, and
      manual departure returns to player-controlled flight.
- [ ] Confirm the next site begins fully offscreen right after service. Pass it, reverse, and
      return; the solid arrow changes right, left, then right and hides whenever any target edge is
      visible. It blinks only while offscreen, is static with reduced motion, and has equivalent
      visually hidden direction text.
- [ ] Continue beyond the target in either direction and through the former synthetic altitude and
      speed thresholds. Confirm that neither position, speed, altitude, nor zero fuel causes a
      crash. At each finite world end, confirm the rail is visible before the lander reaches it and
      that only reconstructed physical contact with that rail causes failure. The camera follows
      continuously left and right without retained-window crashes or changing mission progress.
- [ ] Watch the stars and occasional crescent or ringed planet while traversing and reversing. They
      move with the landscape at visibly slower 24-percent parallax, never affect collision or
      accessibility, and reconstruct without a pop when returning. Confirm crescents read as moons,
      every planet has one or two restrained elliptical rings, each rear-center segment disappears
      behind the planet, and each foreground arc remains visible. Inspect the SVG and confirm one
      sky group with exactly two paths, twenty stars, and one or two landmarks in its five chunks.
- [ ] Start a fresh run and confirm the visual gauge is exactly half full for the 15/30 opening
      reserve/reference. After service, confirm carried reserve is uncapped and becomes the full
      reference. Time 0.9-second agent travel separately from the unchanged 0.3-second refuel and
      1.4-second power sequence; hidden time freezes each and reduced motion completes atomically.
- [ ] From every page footer, confirm the icon navigates directly to `/lander/` without adding a
      fragment. Confirm its accessibility label and hover title are equal and nonempty. Human-review
      their wording, the Lander heading, and the 404 explanation against the canonical
      [shared footer contract](../../docs/sdd/2026-08-07-website/site-shell-lld.md#4-shared-footer-contract),
      [shared DOM contract](../../docs/sdd/2026-08-07-website/brand-and-lander-lld.md#4-shared-dom-and-no-javascript-contract),
      and the current live templates; verify each appears in its intended semantic location.
- [ ] Confirm the visual fuel level drains smoothly without repetitive live announcements. Spend
      fuel on thrust, carry excess through multiple sites, and verify empty fuel produces no thrust.
      At exactly zero, the whole red gauge blinks with normal motion, pauses while the game is
      inactive, and remains a strong static red warning with reduced motion.
- [ ] Crash on terrain, a platform end or underside, the truss or any lattice-column envelope, the
      NOC, and the mast. Normal motion shows one brief compact flash and exactly eight ballistic
      fragments for 600 milliseconds, with no smoke, dust, sound, shake, or page movement. Reduced
      motion reaches the same failed state with no moving debris.
- [ ] Retry after crashes before and after a powered site. Before the first checkpoint, Retry
      restores the same seeded initial approach. Afterward, it starts on the last powered pad,
      relaunches using fuel, and never duplicates the can, award, progress, ratio, or power
      sequence. Depart from the launch-ready pad with ordinary keyboard, vi, pointer, and touch
      flight controls; confirm no native Launch action exists.
- [ ] Fly far enough for the camera and rolling window to move in both directions. At every sampled
      point, count exactly three terrain-layer paths (fill, surface, and physical termini), at most
      three site groups, eight debris nodes, and no more than 76 descendants under `#lander-world`;
      discarded sites do not return as retained history.
- [ ] Exercise straight, turn-only, and combined keyboard thrust plus pointer/touch at half and full
      drag. Confirm steering visibly gimbals both plumes, reduces forward thrust, never exceeds
      straight collective, and keyboard steer owns direction during simultaneous pointer input.
      Neutral powered thrust visibly counter-rotates unequal plumes; engine-off coast does not damp
      rotation. Empty fuel resets plume direction to neutral.
- [ ] Perform a short primary tap whose browser-generated `lostpointercapture` fires synchronously.
      Confirm thrust remains active through 139.999 ms, ends once at 140 ms, and cancel/blur/hide
      tears it down immediately. Repeat with a reused browser pointer ID.
- [ ] Land exactly at the inclusive 2.2 m/s horizontal, 3.6 m/s descent, 18 degree tilt, and 26
      degree/s rotation limits; increase each independently and confirm a crash.
- [ ] Hide and restore the tab during flight, arrow blink, service, launch, and crash. Hidden time
      advances none of them, input and pointer capture clear, and the first visible frame only
      resets timing.
- [ ] Keep the network panel open for the complete three-site run. After initial same-origin HTML,
      CSS, four modules, and SVG loads, the game makes no request and creates no durable storage.
      Confirm the exact 14-file artifact ships `lander-world.js` and its leaf `lander-collision.js`
      separately, with no concatenated duplicate collision authority.
- [ ] Record active frame p95, 100-site retention counts, and direct generation plus the O(1)
      predicted-allowance formula p95/max on the pre-merge Chromium machine. No route search,
      simulation, or proof replay occurs. Required ceilings are 4 ms frame p95, 25 ms generation
      p95, and 50 ms generation maximum.

### Phase 4U automated execution record

- Date: 2026-08-15
- Source: `998e8a77b62659437acf00433441793ef46ef193`
- Browser: repository-selected headless Chromium through the DevTools Protocol
- Tester: Phase 4U implementation gate and qualitative screenshot inspection
- Outcome: PASS; the operator subsequently accepted the exact Phase 4U product in Chrome and Edge

The exact source passed all 103 Node lander tests in 4.90 seconds and all 157 website Python tests,
including the real-Chromium witnesses, in 36.694 seconds. The focused Phase 4Q Chromium module also
passed both tests in 4.87 seconds. Its 100-site lifecycle invokes direct site generation and the
O(1) `quantumCeil(22 + max(0, deckDelta) / 3)` prediction without a route key, schedule, search,
simulation, catalog, or replay. The permanent witness continues to enforce the 4 ms frame-p95, 25 ms
generation-p95, and 50 ms generation-maximum ceilings rather than recording an unobserved timing
distribution here.

The same run emitted 12 distinct qualitative PNGs across seeds 11, 41, and STATIC and all four
required viewport shapes. Inspection covered `1000x780`, `320x780`, true `320x240`, and true mobile
touch `667x320`; the touch case reported inner, document-client, and document-scroll dimensions of
exactly `667x320` with scroll position zero. The straight sharper facets, normalized-band relief,
local decks, native supports, footer actions, and fixed scene remained visible without vertical
growth or scroll. These observations are run evidence, not golden-image assertions.

### Chromium CI reliability correction

- Date: 2026-08-15
- Source: `d7241554d2615ba057f8e56b882a76cc26709299`
- Failure evidence: PR #402 CI runs `31885456373` and `31885547704`
- Outcome: PASS for the corrected harnesses

Both cited Website jobs failed when Chrome's `--dump-dom` process did not exit before the existing
20-second timeout in the responsive long-form geometry test. The page had no corresponding layout
failure; the harness delegated both readiness and process completion to the same browser subprocess,
so a stuck shutdown produced a false CI failure.

The corrected witness retains the original wide/narrow geometry assertions but owns Chrome through
the DevTools Protocol. It waits for the exact navigated URL, complete document, and populated
result; then it explicitly closes the socket and terminates the browser before stopping the server
and removing the isolated profile. The Phase 4M witness now uses the same exact-URL and
complete-document readiness boundary with null-safe document-root access, closing a separate
navigation race observed during local full-suite runs.

Before commit, the responsive geometry witness passed 40 consecutive two-viewport iterations and the
Phase 4M witness passed 30 consecutive real-Chromium iterations without a failure. After the
failure-path mutations were added, the complete website suite passed 161 of 161 tests in 36.618
seconds. Structural cleanup witnesses reject a return to `--dump-dom` and verify DevTools closure,
owned-process termination, bounded readiness, kill fallback, primary-error precedence, and
server-thread cleanup.

### Historical route-proof automated execution record

- Date: 2026-08-10
- Source: `e0db7e225e227c69725709b2fd013a1d3e0d2475`
- Browser and version: Chromium 151.0.7922.108, headless DevTools Protocol
- Operating system: Debian GNU/Linux 12, aarch64; Chromium reported a Linux x86_64 headless user
  agent
- Viewport: 1000 by 900 CSS pixels at device-pixel ratio 1
- Motion preference: normal and reduced
- Tester: isolated fresh-eyes automated browser acceptance
- Outcome: PASS for the then-current route-proof implementation; the record is historical and does
  not accept the Phase 4U direct predicted-allowance implementation

Three legal service contacts were injected into the live controller and then exercised through real
browser animation frames, model transitions, rendering, and player-control return. Fuel progressed
from `30` to `54.75`, `76.638`, and `94.78536`; each can disappeared once; stages 0 through 5 filled
the four bars in order and enabled the antenna at 1,000 milliseconds; each offscreen cue and launch
returned correctly. The 100-site timing witness invoked the real model inside Chromium rather than
claiming 100 manually flown services.

The actual simultaneous worst-case render contained ten terrain paths, three complete sites, eight
crash fragments, five direct world children, and exactly 80 `#lander-world` descendants. Active
frame callback time over 1,250 samples had p95 `0.9 ms`, maximum `26.5 ms`, and mean `0.433 ms`.
Generation plus the source's then-current template selection and proof replay across 100
deterministic services had p95 `7.2 ms`, maximum `15.3 ms`, and mean `6.071 ms`. Retention stayed at
ten chunks and three sites while the camera moved in both directions. After garbage collection,
observed JavaScript heap decreased from 2,123,772 to 1,285,712 bytes, DOM nodes from 484 to 479, and
browser-reported event listeners remained 19 to 19.

The run also passed real CDP keyboard, mouse, and touch events; browser-generated
`lostpointercapture`; every named collision surface; normal and reduced-motion crashes; pre- and
post-checkpoint restart without duplication; actual tab hiding through every timed state; empty-fuel
suppression; Escape teardown; static/dynamic site structure and computed-style parity; no
atmospheric presentation; and shared `/lander/`/`404.html` initialization. Eight initial same-origin
requests produced seven unique resources; actions made no request, no cross-origin request occurred,
and cookies, local/session storage, CacheStorage, and IndexedDB remained empty.

The evidence observes JavaScript heap, DOM nodes, and event listeners rather than operating-system
resident memory or every native browser listener. It does not replace qualitative spoken-screen
reader, physical-touch, Edge, Firefox, or WebKit acceptance. The isolated Chromium process, profile,
and harness were removed; its debug port was closed; the port-8766 preview remained running.

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
      native Exit and Retry names/states, and focus destinations. Record screen reader, browser, and
      versions.
- [ ] After launch, on physical touch hardware, verify start, tap impulse, hold, drag, Exit, Retry,
      scroll outside the active scene, and human motion quality. Record device, operating system,
      browser, and input observations.
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

- [ ] With JavaScript disabled, `/lander/` shows one nonempty, currently approved primary heading
      and the complete static scene. `/404.html` shows its nonempty primary heading followed by its
      nonempty, currently approved explanatory paragraph, a working linked `Agentworks` breadcrumb
      home crumb, and the same scene. Compare the heading and explanation with the canonical
      [shared DOM contract](../../docs/sdd/2026-08-07-website/brand-and-lander-lld.md#4-shared-dom-and-no-javascript-contract)
      and current live templates. There is no error-code eyebrow or body-level home link.
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
      exactly two control lines that do not wrap (keyboard then touch) and native `Exit mission`
      button, keeps `Retry` hidden, hides the start button, and moves focus to the game without
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
- [ ] Escape and native `Exit mission` on the active shell call the same exit operation: cut thrust,
      hide controls and actions, restore settled preflight, and focus the start button without
      scrolling. Escape on a shell link or outside the shell keeps browser behavior.
- [ ] After a crash, both R and native `Retry` restore the last powered-pad checkpoint, carried
      fuel, leg-relative gauge, `Agent Deployed!` banner, and shell focus without recollecting the
      can. Retry is hidden and disabled outside `failed`.
- [ ] In every active state, Exit remains the rail's bottom-right 44 CSS-pixel action and follows
      the shell in game-subtree tab order. Failed state alone inserts the 44 CSS-pixel Retry between
      shell and Exit. Their visible second-line hints are excluded from their accessible names, and
      their shortcut semantics expose `r` and `Escape`.
- [ ] During flying, launch-ready, and failed states, focus the header and breadcrumb and target
      each element and a descendant with Escape, R, Space, Up, arrows, H, and L key-down/key-up
      pairs. Every event remains ordinary page input without prevention or game state, action,
      focus, input, pose, or fuel changes.
- [ ] Focus Exit and Retry and repeat Space, Enter, arrows, H, and L against both the button and
      each nested span. Space or Enter invokes exactly one native action; the other keys neither
      activate an action nor create flight input.
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
- [ ] A tap released within 180 milliseconds and 10 CSS pixels produces one tokenized
      140-millisecond equal-thrust pulse. A new pointer atomically supersedes it; stale capture loss
      or timeout cannot clear the newer gesture.
- [x] Pointer up, cancellation, lost capture, window blur, shell focus loss, tab hiding, contact,
      failure, Escape, restart, and a simulated frame stall all release capture and leave no stuck
      thrust. Repeat each teardown once to confirm it is harmless when already clear.
- [ ] Touch activation of native Exit and terminal-state Retry follows the same controller,
      teardown, model-event, and focus lifecycle as Escape and R. Neither action needs a keyboard.

## Flight, collision, and sequence

- [x] An upright, slow touchdown with both feet inside the marked zone freezes safely and announces
      `Touchdown confirmed. Fuel collected. Deploying agent.`
- [x] A fast, tilted, rotating, one-foot-outside, surface-short, operations-center, or out-of-bounds
      contact enters the restrained failure state. Nothing flashes, shakes, explodes, moves the
      page, changes the home link, or emits sound.
- [ ] Failure announces the reviewed crash copy in the sole live status, shows only Retry beneath
      that centered bordered panel, retains Exit in the final controls rail, and accepts both
      recovery paths.
- [x] After safe touchdown at normal motion, the G bay opens, the terminal-shaped agent descends,
      crosses the surface, and enters the west operations-center door.
- [ ] Power proceeds vertically through four sharp-cornered battery bars at 200 ms intervals, then
      through three bilaterally symmetric signal arches at 1,000, 1,200, and 1,400 ms. There is no
      battery terminal, nub, rounded battery corner, or duplicate payoff. The fixed mast and antenna
      head stay graphite before, during, and after power-up; only the radiating arches gain color.
- [ ] At power completion, the sole live status presents the reviewed deployment copy; the matching
      banner remains centered over the scene at every width without overlapping the complete left
      fuel overlay. The lander, mission clock, pose, and fuel then remain unchanged indefinitely
      until effective player thrust.
- [ ] Space, Up, either combined with vi or arrow steering, held pointer or touch, and short tap all
      depart from the same launch-ready checkpoint through ordinary flight input. Turn-only input
      remains restrained; the first effective collective burns and integrates in that same fixed
      step, and early release receives ordinary gravity. No native Launch action exists.
- [ ] With reduced motion enabled before touchdown, safe contact atomically shows all four battery
      bars, three signal arches, powered NOC, checkpoint, gauge, and banner without automatic
      departure. Physics remains playable.
- [x] Turning reduced motion on during the post-touchdown sequence immediately completes the same
      powered success result. Changing it during flight does not alter physics.
- [x] Retry, Escape, and reload each clear powered state. No mission state survives a new run or
      reload.
- [ ] The vertical left gauge starts the mission half full at 15/30; later legs start full against
      their exact uncapped post-award reserve. It drains against that honest reference, fills
      linearly during the 300-millisecond award transfer, and restores exactly after restart.
      Separate rounded hidden label and value spans form one ordered accessible description segment;
      neither is live or a meter, progress element, or output.
- [ ] Every static and generated site uses one visibly open path containing two chords, twelve
      alternating truss diagonals, and exactly three variable-height two-rail lattice columns with
      ties, alternating braces, butt caps, and round joins. Six rail feet independently meet native
      terrain; collision rejects the exact truss, three complete column, NOC, and mast envelopes.
- [ ] Full steering uses visibly gimbaled 30-degree plumes and lower 0.8 total collective. Turn-only
      axial lift stays below gravity while vacuum coasting retains inertia.

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
- [ ] In touch landscape, the scene fits its container, the active stage alone suppresses touch
      actions, and the controls rail plus browser navigation and zoom remain available outside it.
- [x] On a wide viewport, the scene remains capped at 60 rem with a 25:16 ratio; terrain never
      covers the landing zone, operations center, focus outline, controls, or status.

## Shared shell acceptance

- [x] Home shows the AGW rocket at the accepted historical-baseline size, repository-sourced
      identity, and two-path onboarding chooser. It has no small header mark. Home, Manifesto,
      Security, Lander, and 404 each show one GitHub and one PyPI icon-and-text link in the header
      and exactly three footer destinations: Manifesto, Security, then the icon-only Lander link.
      Only Home's Manual onboarding path repeats the repository destination in the body.
- [ ] In Chrome and Edge before merge, verify at 320 CSS pixels, 400 percent zoom, touch landscape,
      and wide desktop that Home, Manifesto, Security, Lander, and 404 have no page overflow,
      clipped text or navigation, overlap, or fixed-height content loss. Record browser versions,
      viewport, date, and result when available, or record the explicit evidence limitation.
- [ ] After launch, repeat that complete shared-shell matrix in Firefox and WebKit and record the
      browser versions, viewport, date, and result.
- [x] With JavaScript disabled, Home exposes both `via Agent` and Manual onboarding content,
      Manifesto and Security retain all content and links, Lander retains its heading and named
      scene, and 404 retains its message, named scene, and breadcrumb route-home action. No surface
      depends on terminal familiarity.
- [ ] Keyboard-only traversal reaches each visible skip link, breadcrumb home crumb, GitHub, PyPI,
      both Home tabs, the icon-only copy button, all three footer links, and both Lander/404 start
      controls in source order with a visible focus outline. Arrow, Home, and End keys change tabs;
      copy success or failure does not move focus. The copy and footer rocket targets are not
      clipped.
- [ ] A screen reader reports each page title, header, main, footer, one `h1`, nested section
      headings, breadcrumb current state, named navigation, visible CTA labels without decorative
      icon noise, sourced links, copy status, and 404 status/focus behavior. Initial game controls
      remain hidden.
- [ ] With reduced motion enabled, Home, Manifesto, and Security remain motion-free and 404 follows
      the existing no-cue, direct-success contract. Human touch checks confirm page scroll outside
      the active scene and tap, hold, and drag behavior inside it.
- [ ] In a clean context, a visitor identifies what Agentworks is, sees and can manually select the
      canonical bootstrap, and can copy it when clipboard writing is available. Confirm the copied
      bytes match `packaging/agentworks/agent-onboarding-prompt.md`; record timing and any
      intervention.
