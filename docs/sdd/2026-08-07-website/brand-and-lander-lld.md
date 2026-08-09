# LLD: AGW Brand and Lunar Deployment Lander

<!-- cspell:ignore focusout keyup pointerdown unitless -->

- Status: Phase 4C amendment approved for implementation
- Date: 2026-08-08
- FRD: `frd.md`, specifically R6-R9 and R15-R18
- HLA: `hla.md`, specifically D5 and D7
- Selected geometry: `logo-concept-10-twin-flame.svg`

## 1. Scope

This LLD pins the selected asset and shared Lander/404 game, excluding main-page, deployment, and
DNS design. Use plain HTML, CSS, SVG, and JavaScript. A **run** spans START to restart, exit, or
reload; **commanded thrust** is the post-input/fuel engine value shared by physics/plumes; **mission
time** excludes hidden time. The semantic 404 and breadcrumb home link remain independent of the
game subtree; the dedicated Lander shell presents that same subtree deliberately.

## 2. Permanent files and ownership

Implementation uses these permanent names:

| File                                        | Responsibility                                                  |
| ------------------------------------------- | --------------------------------------------------------------- |
| `website/assets/agw-rocket.svg`             | Sole source for the selected A, G, W, and twin-plume paths      |
| `website/templates/404.html`                | Semantic 404 shell with the shared game placeholder             |
| `website/templates/lander.html`             | Dedicated Lander shell with the shared game placeholder         |
| `website/templates/lander-game.html`        | Sole source for the complete reusable game subtree              |
| `website/build.py`                          | Standard-library base validation, rendering, and asset copying  |
| `website/static/lander.css`                 | Scene layout, state selectors, focus, cue, and SVG presentation |
| `website/static/lander-model.js`            | Pure state, scheduler, physics, collision, and plume functions  |
| `website/static/lander-game.js`             | DOM, clock/input adapters, focus, lifecycle, and rendering      |
| `website/tests/lander-model.test.mjs`       | Built-in `node:test` unit and deterministic-vector coverage     |
| `website/tests/test_lander_404.py`          | Standard-library build/no-JS/forbidden-surface checks           |
| `website/tests/lander-browser-checklist.md` | Package-free manual browser and accessibility acceptance        |

`lander-game.js` alone imports the model and owns the frame and pointer; no scheduler is duplicated.
The production build-time seam is `website/build.py --repo-root ROOT --output OUT --site-base BASE`.
It renders the complete linked ten-file site; game work serves `/lander/` from that artifact and
fallback acceptance also exercises `/404.html`. `BASE` is a slash-bounded URL path; `/` and
`/agentworks/` pass. A closed ASCII segment grammar rejects encoding, whitespace, controls, HTML
delimiters, URL components, backslashes, `//`, and dot segments. Output beneath the repository is
rejected before staging. The sole token `{{SITE_BASE}}` prefixes home and local asset URLs; missing
uses or tokens fail. The builder has no partial-output mode.

## 3. AGW SVG contract

### 3.1 Coordinate system and stable groups

`agw-rocket.svg` retains `viewBox="0 0 240 520"`. It has no width, height, font, script, animation,
external reference, or embedded bitmap. Its root and reusable descendants use these globally
prefixed IDs:

```text
agw-rocket: agw-rocket-title, agw-rocket-description, agw-plumes, agw-mark
agw-plumes: agw-engine-left, agw-engine-right
agw-engine-left: agw-left-cool-edge, agw-left-warm-middle, agw-left-hot-core
agw-engine-right: agw-right-cool-edge, agw-right-warm-middle, agw-right-hot-core
agw-mark: agw-letter-w, agw-letter-g, agw-letter-a
```

Paths and transforms are byte-identical to the concept; only IDs, root metadata, and formatting may
change. The asset is a named image titled `Agentworks AGW rocket with twin layered flames`. The
`aria-hidden` `#mission-lander` references that source with same-origin external uses:
`#mission-mark` uses `{{SITE_BASE}}assets/agw-rocket.svg#agw-mark`; `#mission-left-engine` and
`#mission-right-engine` use their corresponding `agw-engine-*` fragments.

The body color is `#292b30`. Each engine preserves, from outside to inside, `#d94a1e`, `#ff7a00`,
and `#ffe09a`. CSS transforms only the two `#mission-*-engine` use elements, never their nested
temperature layers.

### 3.2 Lander reference point

The physics pose `(x, y, angle)` refers to the midpoint between the W's two lowest solid points at
asset coordinate `(120, 415)`. The inline scene applies transforms in this order:

```text
translate(sceneX, sceneY) rotate(angleDeg) scale(0.16) translate(-120, -415)
```

Positive world `x` is right, positive world `y` is up, and positive `angle` is clockwise, matching
SVG rotation. The collision feet are model points `(-1.6 m, 0)` and `(1.6 m, 0)` relative to the
reference point. The lander's solid hull is the rectangle from `(-1.6,0)` to `(1.6,6.5)` transformed
by its pose. Plumes are visual only and do not enlarge the hull.

Pure `transformLocalPoint(pose,lx,ly)` returns `worldX=x+lx*cos(a)+ly*sin(a)` and
`worldY=y-lx*sin(a)+ly*cos(a)` for clockwise degrees; feet, hull, render, and G opening use it.

## 4. Static Lander/404 and DOM contract

The Lander and 404 shells each render the shared `header`, `main`, and `footer` landmarks. The 404
header breadcrumb is its sole visible route-home action. One reviewed `lander-game.html` fragment
owns the following stable subtree and is rendered byte-equivalently into both shells:

```text
<section id="lander-game" aria-label="Lunar deployment scene">
  <div id="lander-scene-shell">
    <svg id="lander-scene" viewBox="0 0 1000 640" ...>...</svg>
    <button id="lander-start" type="button" hidden aria-label="Start lunar deployment mission"></button>
  </div>
  <p id="lander-controls" hidden>...</p>
  <p id="lander-status" role="status" aria-live="polite" aria-atomic="true"></p>
</section>
```

Each `main` begins with a `.page-heading` containing only its reviewed `h1`: `Lunar deployment` on
the dedicated route and `Page not found` on 404. The 404 then retains
`<p id="not-found-message">...</p>` before the shared subtree. Both use `.detail-main` and the
game-specific compact gap, with no eyebrow, error-code, provenance, or other pre-title label.

`#lander-controls` is the only control copy, stays hidden until START, and then reads exactly:
`Thrust: Space or Up. Turn: Left/H or Right/L. Escape exits. R restarts after success or failure.`

The zero-angle preflight asset occupies scene `x=[285.92,314.08]`, `y=[163.2,243.36]`, including
nose, body, engines, and settled plumes. Center the transparent start button at `(30%,31.7625%)` and
size it `max(44px,2.816%)` by `max(44px,12.525%)`. This covers the full rendered silhouette at every
shell size with at least 44 CSS pixels in each dimension. Only `:focus-visible` draws its three
pixel outline and two pixel offset.

On start, the controller hides and disables `#lander-start`, gives `#lander-scene-shell`
`tabindex="0"`, `role="application"`, `aria-label="Lunar deployment game"`, and
`aria-describedby="lander-controls lander-status"`, then focuses it with `preventScroll: true`. On
exit it removes those active attributes, restores `tabindex="-1"`, reveals and enables the start
button, hides the controls, and focuses the start button without scrolling.

Before activation the SVG is a named image whose description mentions the lander, surface, zone, and
dark NOC, but no controls. While the shell is an application, the SVG is `aria-hidden`; status
conveys changes and no SVG descendant is separately exposed.

With JavaScript unavailable, the start button and controls remain hidden. The static named scene and
each page heading remain visible and usable; 404 also retains its explanatory text and breadcrumb
home anchor. No CSS selector depends on a JavaScript-added class to show shell content or the
breadcrumb.

## 5. Responsive scene geometry

The scene uses the pinned view box, `preserveAspectRatio="xMidYMid meet"`, ratio `25 / 16`, and
`width: min(100%, 60rem)`, with no minimum width. `overflow: hidden` clips only departing artwork.
It cannot cause page overflow at 320 CSS pixels or 400 percent zoom; collision stays in view-box
coordinates.

World-to-scene mapping is:

```text
sceneX = worldX * 10
sceneY = 548 - worldY * 10
```

Surface collision is world `y=0`, scene `y=548`. That exact horizon spans the zone through the NOC,
so terrain cannot cover the operational geometry.

Pinned game geometry, in world metres, is:

| Object                    | Geometry                                                 |
| ------------------------- | -------------------------------------------------------- |
| Playable horizontal bound | Lander reference `x` in `[7, 93]`                        |
| Playable vertical bound   | Lander reference `y <= 48`; `y <= 0` is ground contact   |
| Landing zone              | Surface segment `x` in `[18, 42]`                        |
| Zone marker               | Scene line `(180, 548)` to `(420, 548)`, two units thick |
| NOC west module           | Rectangle `x=[54, 59]`, `y=[0, 4.2]`                     |
| NOC server module         | Rectangle `x=[60, 66]`, `y=[0, 6.8]`                     |
| NOC east module           | Rectangle `x=[67, 72]`, `y=[0, 4.8]`                     |
| NOC entry                 | West face centered at `(54, 1.1)`                        |
| Antenna                   | Mast from `(69.5, 4.8)` to `(69.5, 8.0)`                 |

NOC is `#20232a`; inactive features `#3b3f47`; windows `#ffe09a`; status `#7de2c5`; antenna/signals
`#d94a1e`, with computed `3.788:1` contrast on lunar `#f5f2e8`; terrain `#d7d2c4`; outlines
`#4b4e55`. Powered state also adds window bars, solid status lights, and two static signal arcs.

The landing zone is visually distinct and left of the NOC. Their 12 metre gap has no collision
geometry. The scaled radius is `66.3121` scene units; `[7,93]` maps to `x=[70,930]`, and `y=48` maps
to scene `y=68`, keeping the rotating mark visible.

## 6. CSS state contract

`#lander-game` is the only state-bearing DOM root. The controller sets:

- `data-mission-state` to one state name from section 7;
- `data-noc-power="off|on"`;
- `data-noc-stage="0|1|2|3|4"` for west, server, east, and antenna progression;
- `data-cue="running|settled"`;
- `data-paused="true|false"`;
- CSS custom properties `--lander-x`, `--lander-y`, `--lander-angle`, `--left-plume-scale`,
  `--right-plume-scale`, `--left-plume-opacity`, and `--right-plume-opacity`.

`--lander-x`/`--lander-y` are scene `px`, `--lander-angle` is `deg`, and plume scale/opacity are
unitless. Render writes them once after a frame's steps; CSS never infers model state.

`lander.css` selects those attributes and structural IDs. Its only keyframes are `agw-preflight-cue`
and `agw-agent-route`. Controller clocks, never CSS events, advance and test state.
`data-paused="true"` pauses every active keyframe; powered antenna arcs remain static.

Initial enhancement sets `data-cue="running"` once for a 2.4-second, three-pulse plume scale from
`0.08` to `0.28`, then `settled`. Reduced motion and START settle it immediately. EXIT restores
settled preflight and never replays it; only reload creates a new cue opportunity.

Reduced motion disables animations/transitions. Settled preflight variables keep plumes at `0.08`;
no media rule overrides live flight plume commands. Essential physics remains, while the decorative
post-touchdown sequence uses section 11's shortcut.

## 7. Mission state machine

The machine is named `DeploymentMission`. Its states and only legal transitions are:

| From                         | Event                               | To          |
| ---------------------------- | ----------------------------------- | ----------- |
| `preflight`                  | `START`                             | `flying`    |
| `flying`                     | `SAFE_CONTACT`                      | `landed`    |
| `flying`                     | `SAFE_CONTACT` with reduced motion  | `succeeded` |
| `flying`                     | `UNSAFE_CONTACT` or `OUT_OF_BOUNDS` | `failed`    |
| `landed`                     | `LANDING_SETTLED`                   | `deploying` |
| `deploying`                  | `AGENT_ENTERED`                     | `powering`  |
| `powering`                   | `NOC_POWERED`                       | `departing` |
| `departing`                  | `LANDER_DEPARTED`                   | `succeeded` |
| `failed` or `succeeded`      | `RESTART`                           | `flying`    |
| Any state except `preflight` | `EXIT`                              | `preflight` |

Reduced-motion `SAFE_CONTACT` atomically records touchdown, powers the NOC, and succeeds. Illegal
events return the unchanged model.

Entry invariants: preflight has initial pose, NOC/input off, controls hidden; flying resets clock
and enables collision/controls; landed freezes and zeros input; deploying opens bay/agent; powering
is monotonic; departing uses powered-NOC scripted pose; succeeded keeps NOC/exact status; failed
freezes harmlessly with NOC/engines off.

`START` and `RESTART` call `createFlightModel()`, which returns a new `flying` model with pinned
pose/fuel and reset NOC, bay, agent, and signals. The transition also resets controller clocks,
accumulator, inputs, and pointer. `EXIT` instead creates the preflight presentation model.

## 8. Simulation contract

### 8.1 Units and constants

The model uses metres, seconds, metres per second, degrees, and degrees per second. Constants are
named exports and are not configurable at runtime:

```js
export const STEP_SECONDS = 1 / 120;
export const MAX_FRAME_SECONDS = 0.1;
export const MAX_CATCH_UP_STEPS = 12;
export const GRAVITY = 3.0; // m/s^2 downward
export const ENGINE_ACCELERATION = 4.2; // m/s^2 per engine at command 1
export const TORQUE_ACCELERATION = 70.0; // deg/s^2 at unit differential
export const FUEL_CAPACITY = 30.0; // engine-seconds
export const FUEL_FLOW = 1.0; // units/s per engine at command 1
export const INITIAL_X = 30.0;
export const INITIAL_Y = 32.0;
export const INITIAL_VX = 0.8;
export const INITIAL_VY = -0.4;
export const INITIAL_ANGLE = 0.0;
export const INITIAL_ANGULAR_VELOCITY = 0.0;
export const MAX_PLAYABLE_Y = 48.0;
export const MAX_LANDING_HORIZONTAL_SPEED = 1.4;
export const MAX_LANDING_DESCENT_SPEED = 2.2;
export const MAX_LANDING_ANGLE = 8.0;
export const MAX_LANDING_ANGULAR_SPEED = 12.0;
```

Fuel has no mass effect. Burn is `FUEL_FLOW * (left + right) * STEP_SECONDS`. If fuel is smaller,
scale both engines by `fuel / requestedBurn`, exhausting it without favoring an engine. These
effective values drive physics and plumes; zero fuel means zero thrust.

### 8.2 Input mixing

Digital controls produce:

```text
collective = 0.72 when Space or Up is held, otherwise 0
leftBias = 0.45 when Left or H is held, otherwise 0
rightBias = 0.45 when Right or L is held, otherwise 0
keyboardLeft = clamp(collective + rightBias, 0, 1)
keyboardRight = clamp(collective + leftBias, 0, 1)
```

Left/H fires right and turns left; Right/L fires left and turns right. Both directions yield equal
thrust. Each final engine request is `max(keyboard, pointer)`, not their sum.

### 8.3 Integrator

Each step first applies queued input events through the step-end mission timestamp, scales thrust
for fuel, then uses the pre-step angle for acceleration:

```text
total = ENGINE_ACCELERATION * (left + right)
ax = total * sin(angle radians)
ay = total * cos(angle radians) - GRAVITY
angularAcceleration = TORQUE_ACCELERATION * (left - right)

vx += ax * dt
vy += ay * dt
angularVelocity += angularAcceleration * dt
x += vx * dt
y += vy * dt
angle = normalizeDegrees(angle + angularVelocity * dt)
```

This is semi-implicit Euler, with no drag, damping, clamp, bounce, randomness, or variable mass.
`normalizeDegrees` returns `[-180, 180)` and state is not rounded between steps.

### 8.4 Fixed-step and catch-up

`lander-model.js` exports side-effect-free `createSimulationClock(timestamp)`,
`enqueueInputEdge(clock, edge)`, and `advanceSimulation(clock, model, timestamp)`. Their immutable
clock owns timestamp, accumulator, integer cursor, sequence, and queue. Controller and Node tests
use these same exports; there is no second scheduler.

The first advance renders without stepping. Each later `advanceSimulation`:

1. Compute `frameSeconds = (timestamp - previousTimestamp) / 1000` and store the new timestamp.
2. If `frameSeconds < 0` or `frameSeconds > MAX_FRAME_SECONDS`, return unchanged model plus a reset
   clock and `discarded: true`, with no step.
3. Otherwise add `frameSeconds` to the accumulator. While `accumulator + 1e-12 >= STEP_SECONDS`,
   take a step, subtract `STEP_SECONDS`, and stop after 12 steps.
4. If numerical residue is within `1e-12` of zero, set it to zero. Render once.

Order `performance.now()` edges by timestamp then sequence and apply through integer step cursor
plus `1e-9 ms`. On `discarded`, invoke pointer teardown, clear held input, and render unchanged
state; the reset cursor advances without mission time, preserving frame-schedule independence.

When hidden, cancel the frame, clear input/capture and accumulator, and set `data-paused="true"`.
When visible, the first new frame only resets time; hidden time never accumulates.

## 9. Contact, landing envelope, and failure

After each integration step, transform the feet and solid hull by the new pose. First ground contact
occurs when any hull vertex has world `y <= 0`; NOC contact is polygon-rectangle intersection.
Classification uses post-step velocities and angle before clamping.

A contact is safe only when all comparisons below are true, including equality at each limit:

```text
both transformed feet have x in [18, 42]
vy <= 0
abs(vy) <= 2.2
abs(vx) <= 1.4
abs(normalizeDegrees(angle)) <= 8.0
abs(angularVelocity) <= 12.0
```

Classify the raw post-step pose in this order: NOC `UNSAFE_CONTACT`; ground as safe touchdown or
`UNSAFE_CONTACT`/`surface`; then `x` or upper-`y` `OUT_OF_BOUNDS`. Ground crossing is not a bound.
Settle safe contact from raw pose with its lower foot at `y=0`; clamp only frozen failures.
Antenna/signals do not collide, departure ignores bounds, and fuel is not a landing predicate.

Failure status is exactly `Landing unsuccessful. Press R to restart or Escape to exit.` There is no
shake, flash, explosion, layout movement, or breadcrumb change.

## 10. Exact input behavior

### 10.1 Keyboard

Match Space/arrows by `code` and `h`, `l`, `r` by lowercased `key`.

Preflight starts only for non-repeated, unmodified `code === "Space"` targeted at `body`, game,
shell, or scene. Exclude any anchor, button, form control, editable element, or descendant. Consume
only the accepted event. Its START seeds held physical `Space` and a same-timestamp collective-down
edge; document keyup ends it even after focus moves to the shell. Native start-button click,
including keyboard click, starts without thrust.

In `flying`, accept Space, Up, Left, Right, H, and L without Control, Alt, or Meta only on the
active shell path; Shift does not alter mapping. Consume repeats but queue only first edges. Release
an accepted held code even after focus moves. Track aliases by physical code, so releasing Up leaves
Space-held collective active.

Unmodified Escape exits any non-preflight state only on the active shell path, cuts engines, and
focuses the restored start button. Breadcrumb-link Escape and every event outside that path retain
browser behavior. Unmodified `r` restarts only `failed` or `succeeded` on the shell path.

Window blur, shell focusout, exit, restart, contact, failure, and hide clear keys and queue zero
input. Focus loss does not pause or change state.

### 10.2 Pointer Events

The preflight lander's ordinary click starts without thrust. In `flying`, accept only the primary
pointer and mouse button 0. Pointer down is consumed and captured, records client position/time, and
immediately commands equal `0.72`; ignore a second pointer.

For a captured pointer, horizontal displacement is measured in CSS pixels from pointerdown:

```text
deadZone = max(10 px, rendered scene width * 0.01)
fullBiasDistance = max(56 px, rendered scene width * 0.18)
bias = sign(dx) * clamp((abs(dx) - deadZone) / (fullBiasDistance - deadZone), 0, 1)
pointerLeft = clamp(0.72 + 0.28 * bias, 0, 1)
pointerRight = clamp(0.72 - 0.28 * bias, 0, 1)
```

Positive drag biases left and turns right; negative drag biases right and turns left. Ignore
vertical travel. Client-space distance keeps behavior responsive.

A tap is pointer up within 180 ms and 10 CSS pixels. Its equal `0.72` pulse lasts at least 140 ms
from down, using a timer that queues its release. After early tap-up, release capture but retain the
logical token until pulse end and ignore another down. Idempotent `teardownPointer()` releases owned
capture; clears active/logical tokens, pulse timer, and queued gesture edges; then queues zero
input. Pointer up/cancel, lost capture, stall discard, exit, restart, contact, failure, blur, hide,
and `destroy()` use it (ordinary up may honor the minimum; every other cause does not).

Only the flying shell gets `touch-action: none` and suppression. Elsewhere, scrolling, zoom,
selection, and links work normally. Clear the active pointer before state callbacks.

## 11. Plumes and successful deployment

### 11.1 Plume mapping

For commanded engine thrust `u` in `[0, 1]`, model export `plumeForThrust(u)` returns:

```text
scaleY = 0.08 + 0.92 * u
opacity = 0.25 + 0.75 * u
```

Write external uses independently: scale `#mission-left-engine` around `(82,401)` and right around
`(158,401)`. Plumes do not affect collision/layout. Scales: `0 -> 0.08`, `0.5 -> 0.54`, `1 -> 1`.

### 11.2 G-bay, agent route, NOC power, and departure

Normal-motion sequence times are mission-clock durations and do not depend on frame count:

1. `landed`, 300 ms: the G opening gains a visible graphite bay lip. Status is
   `Touchdown confirmed. Deploying agent.`
2. `deploying`, 2200 ms: a 0.9 metre terminal-shaped agent appears at lander-local G opening
   `(1.136 m, 2.8 m)`, transformed from the touchdown pose by `transformLocalPoint`. From 0 to 350
   ms it descends to the surface. From 350 to 2000 ms it moves linearly to the NOC entry at
   `(54, 1.1)`. From 2000 to 2200 ms it enters and becomes hidden.
3. `powering`, 1000 ms: the west window powers at 200 ms, server status bars at 400 ms, east window
   at 600 ms, and the two static antenna arcs at 800 ms. Once on, each feature remains on. At 1000
   ms `data-noc-power` becomes `on` and remains on.
4. `departing`, 1800 ms: both scripted engine commands are `0.82`. The lander moves linearly from
   its touchdown pose to `(touchdownX + 6 m, 62 m)` while angle approaches zero by the shortest
   signed path. Physics and collision remain disabled. At 1800 ms the lander is hidden, engines are
   zero, state becomes `succeeded`, and the live status is set in one DOM write to exactly
   `Agent deployed. Mission continues.`

The decorative, hidden agent has a terminal body, legs, and path-drawn `>_`, with no font. Bay and
agent overlay the unchanged G's existing opening.

Reduced-motion safe contact skips bay, route, power sequence, and departure in one task: hide
lander/agent, show powered NOC and static arcs, succeed, and set final status. Turning the
preference on mid-sequence uses this shortcut; changing it in flight does not alter physics.

## 12. Per-run state, focus, and lifecycle

Section 14's forbidden surfaces enforce in-memory runs; reload starts fresh. Home precedes game;
never intercept Tab or move/trap focus. Restart retains shell focus. `destroy()` cancels
clocks/listeners, clears input/capture/active ARIA, status, and thrust, hides its now-dead start
control, and leaves the static no-JavaScript recovery intact.

## 13. Deterministic vectors

Unit tests use tolerance `1e-10` unless an exact string, integer, state, or Boolean is specified.
Each vector starts from the listed values rather than the gameplay initial state unless stated.

| Vector                   | Input                                                                                             | Expected result                                                                                                                                                    |
| ------------------------ | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Gravity for 120 steps    | `(x,y,vx,vy)=(10,30,0,0)`, angle zero, engines zero                                               | `x=10`, `y=28.4875`, `vx=0`, `vy=-3`, fuel `30`                                                                                                                    |
| Collective for 120 steps | Same pose, both engines `0.72`                                                                    | `y=31.5367`, `vy=3.048`, angle and `x` unchanged, fuel `28.56`                                                                                                     |
| One right-engine step    | `(x,y,vx,vy)=(10,30,0,0)`, left `0`, right `1`                                                    | `x=10`, `y=30.0000833333333`, `vy=0.01`, angular velocity `-0.583333333333`, angle `-0.00486111111111`, fuel `29.9916666666667`                                    |
| Exhaustion scaling       | Fuel `0.005`, one step, left and right `1`                                                        | Each effective engine is `0.3`, fuel is exactly `0`                                                                                                                |
| Plumes                   | Thrust `0`, `0.5`, `1`                                                                            | Scales `0.08`, `0.54`, `1`; opacities `0.25`, `0.625`, `1`                                                                                                         |
| Safe inclusive edge      | Both feet in zone; `vx=1.4`, `vy=-2.2`, angle `-8`, angular velocity `12`                         | Safe contact                                                                                                                                                       |
| Unsafe epsilon           | Repeat edge vector with any one magnitude larger by `1e-9`                                        | Failed contact                                                                                                                                                     |
| Frame equivalence        | Gameplay initial state, no input, callbacks through 1000 ms at exact 30, 60, and 120 Hz schedules | 120 steps; `x=30.8`, `y=30.0875`, `vx=0.8`, `vy=-3.4`, fuel `30`, angle and angular velocity zero                                                                  |
| Timestamped input        | Gameplay initial state; edges at 125, 375, 625, and 875 ms as below                               | `x=30.789294822951447`, `y=32.5627822785585`, `vx=0.7612478269876443`, `vy=1.4296050729753735`, angle `-2.5112500000010414`, angular velocity `-4.9`, fuel `28.85` |
| Stall discard            | Callback at 0, then 100.000001 ms, then 108.333335 ms                                             | No first-gap steps; exactly one step after resume                                                                                                                  |

Schedules include an explicit final 1000 ms callback. A second vector queues collective down at 125
ms, Left down at 375, Left up at 625, and collective up at 875. The expected row must match across
all schedules.

## 14. Verification matrix

Log manual rows in the checklist with date, browser/version, viewport, motion setting, and outcome.
The exact root-base demo is
`python3 website/build.py --repo-root . --output /tmp/agentworks-site-demo --site-base /`, then
`python3 -m http.server --directory /tmp/agentworks-site-demo 8000`. Open `/lander/` for game work
and `/404.html` for fallback acceptance. The output is the complete ten-file artifact: `index.html`,
`manifesto/index.html`, `security/index.html`, `lander/index.html`, `404.html`,
`assets/agw-rocket.svg`, `static/site.css`, `static/lander.css`, `static/lander-model.js`, and
`static/lander-game.js`.

| Layer                                                               | Required coverage                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `node --test website/tests/lander-model.test.mjs`                   | One pure scheduler; state/event matrix; Space held through START; keyboard/pointer mixing; vectors; fuel; transformed geometry; landing/playable bounds; NOC/surface/bound ties and independent clamps; frame/input ties, catch-up/stall; plume/sequence; one-shot cue/EXIT; reset; exact status                                                                                                                                                                                                                                                                                          |
| `python -m unittest discover -s website/tests -p 'test_*.py'`       | Builder rejects invalid bases, renders root/project bases to the exact output set, enforces the shared Lander/404 subtree and closed placeholder vocabulary, and checks template/rendered no-JS semantics, external SVG IDs, CSS units, antenna contrast `3.788:1`, and forbidden `fetch`, `XMLHttpRequest`, `WebSocket`, `EventSource`, `navigator.sendBeacon`, `document.cookie`, `Cache`/`CacheStorage`/`caches`, `ServiceWorker`/`navigator.serviceWorker`, `location.href`/`assign`/`replace`, `history.pushState`/`replaceState`, `localStorage`, `sessionStorage`, and `indexedDB` |
| Manual Chromium and Firefox acceptance; Safari or WebKit at go-live | No-JS recovery; one-shot cue; Space hold-through-start; native start/focus; shell-only Escape/home independence; keys; full pointer/stall teardown; safe/failure/success/reset; hidden time; request log labels initial same-origin document/module/CSS/SVG loads and proves zero game-initiated requests                                                                                                                                                                                                                                                                                 |
| Manual responsive acceptance                                        | 320 CSS pixels, 400 percent zoom, touch landscape, and wide viewport: no page overflow, clipped controls, covered breadcrumb home link, or start target below the pinned full-silhouette bounds                                                                                                                                                                                                                                                                                                                                                                                           |
| Standard-library accessibility assertions                           | Landmarks, heading order, duplicate IDs, names, hidden state, live region, focusable elements, and fixed-token 4.5:1 text and 3:1 necessary-graphic contrast                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Manual keyboard and screen-reader acceptance                        | Logical tab/focus, no trap or intercepted Tab, restart/Escape focus, static description, start name, revealed controls, restrained intermediate announcements, failure and exact success, silent decorative SVG                                                                                                                                                                                                                                                                                                                                                                           |

Reduced-motion acceptance additionally proves no cue, agent travel, sequential power animation, CSS
transition, or departure, while playable physics and the complete success result remain.

## 15. Traceability

| Requirement or decision                                                               | Pinned by                      |
| ------------------------------------------------------------------------------------- | ------------------------------ |
| R6, D5: custom graphite AGW and original twin layered plumes                          | Sections 2, 3, 6, and 11.1     |
| R7, D7: shared static Lander/404 game, hidden reveal, Space and activation start      | Sections 4, 6, and 10.1        |
| R8, D7: keyboard/pointer thrust, physics, landing, deployment, powered NOC, departure | Sections 5 and 7-11            |
| R9, D5/D7: no-JS recovery, no nonessential motion, pause/exit, ephemeral state        | Sections 4, 6, 8.4, 12, and 14 |
| AC5: semantic fallback, home path, and hidden preflight controls                      | Sections 4, 6, and 14          |
| AC6: focus, keyboard/pointer controls, and independent plumes                         | Sections 4, 10, 11.1, and 14   |
| AC7: safe/failure states, deployment, persistent-run NOC, and exact status            | Sections 7, 9, 11.2, and 14    |
| AC8: deterministic schedules, lifecycle, motion, accessibility, and narrow screens    | Sections 8.4, 10, and 14       |
| AC19: one byte-equivalent shared game subtree on Lander and 404                       | Sections 2, 4, and 14          |

Implementation must treat this LLD as temporary design input. Permanent source and tests stand on
their own and do not link back to this SDD path.
