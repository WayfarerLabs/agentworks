# LLD: AGW Brand and Continuous Lunar Deployment Lander

<!-- cspell:ignore focusout imul keyup pointerdown PRNG repower -->
<!-- cspell:ignore lerp Minkowski nonfallback overspeed subinterval unhashed unmarginated -->
<!-- cspell:ignore substep unitless uint32 quantized quantization -->

- Status: Continuous Lander refinement designed; implementation pending
- Date: 2026-08-10
- FRD: `frd.md`, specifically R6-R9 and R15-R23
- HLA: `hla.md`, specifically D5 and D7
- Selected geometry: `logo-concept-10-twin-flame.svg`

## 1. Scope and terms

This LLD preserves the selected brand and replaces the one-shot game with the continuous Lander
defined by R7-R9 and R21-R23. It excludes main-page, onboarding, deployment, and DNS design. Use
plain HTML, CSS, SVG, and JavaScript.

A **run** begins at START and ends at Exit or reload. A run contains successive **legs**, each from
one checkpoint or the initial approach to one target site. A **site** is one platform, gas can, and
NOC. **Commanded thrust** is the post-input, post-fuel engine value shared by physics and plumes.
**Mission time** excludes hidden time. A **demonstrated minimum** is the smallest fuel allowance, at
the pinned fuel quantum, that completes one checked-in constructive reference schedule. It is not a
global mathematical optimum over all possible controls.

The semantic 404, breadcrumb home link, and dedicated Lander shell remain independent of the game
subtree. Both shells render the same game fragment.

## 2. Permanent files and module boundaries

Implementation uses these permanent names:

| File                                                   | Responsibility                                                  |
| ------------------------------------------------------ | --------------------------------------------------------------- |
| `website/assets/agw-rocket.svg`                        | Canonical selected A, G, W, and twin-plume geometry             |
| `website/assets/agw-favicon.svg`                       | Flame-free browser projection of the selected A/G/W mark        |
| `website/templates/404.html`                           | Semantic 404 shell with the shared game placeholder             |
| `website/templates/lander.html`                        | Dedicated Lander shell with the shared game placeholder         |
| `website/templates/lander-game.html`                   | Sole source for the complete reusable game subtree              |
| `website/build.py`                                     | Standard-library rendering, validation, and asset copying       |
| `website/site_game_validation.py`                      | Focused game DOM, module-closure, and manifest validation       |
| `website/static/lander.css`                            | Scene layout, state selectors, focus, and SVG presentation      |
| `website/static/lander-world.js`                       | Pure seed, terrain, site, geometry, and window functions        |
| `website/static/lander-model.js`                       | Flight/run state, physics, contact, and constructive proofs     |
| `website/static/lander-game.js`                        | DOM, clock/input, camera, focus, lifecycle, and rendering       |
| `website/tools/derive_lander_routes.mjs`               | Independent, deterministic route-fixture derivation CLI         |
| `website/tests/fixtures/lander-route-geometry-v1.json` | Canonical template geometry and envelope input                  |
| `website/tests/fixtures/lander-route-derived-v1.json`  | Reviewed independent schedules and witness output               |
| `website/tests/lander-world.test.mjs`                  | Seeded world, window, site, and template vectors                |
| `website/tests/lander-model.test.mjs`                  | Scheduler, physics, mission, fuel, and checkpoint vectors       |
| `website/tests/test_lander_404.py`                     | Build, DOM, no-JS, and forbidden-surface checks                 |
| `website/tests/lander-browser-checklist.md`            | Package-free browser, performance, and accessibility acceptance |

The shipped artifact grows by exactly one file, `static/lander-world.js`. Tool and test files are
not in the artifact. `lander-game.js` imports only `lander-model.js`. `lander-model.js` imports only
pure exports from `lander-world.js`. `lander-world.js` imports neither production module, reads no
DOM, clock, storage, or ambient randomness, and owns no mutable singleton. No other production
module imports upward through this chain.

The model is the sole mutable run authority. One run aggregate owns physics, fuel, mission state,
seed, generator cursor, retained sites, active and target IDs, route proof, checkpoint, and crash
debris. The controller owns browser listeners, the animation frame, focus, pointer capture, CSS
projection, and entropy acquisition. It must not keep a second site, fuel, checkpoint, or mission
copy.

Prefer each focused production, tool, or test module at or below 500 lines; every authored source
must remain below 1,000. `website/site_validation.py` is already near the hard ceiling. It imports
`validate_game_contract` from `site_game_validation.py` and passes the rendered pages, asset
manifest, and site base into that focused authority. The helper imports only the standard library,
never imports `site_validation.py`, and owns all new game-subtree, game-module closure, and exact
game-manifest rules. `test_lander_404.py` exercises the helper. The helper is build source, not a
shipped site file, so the browser artifact remains exactly 12 files. Splitting follows authority,
not line compression.

The build seam remains `website/build.py --repo-root ROOT --output OUT --site-base BASE`. It emits
only the complete linked site. The sole `{{SITE_BASE}}` token prefixes local links and imports.
Missing tokens, unresolved references, or any output other than the exact manifest fail before
replacement.

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

Paths and transforms are byte-identical to the selected concept; only IDs, root metadata, and
formatting may change. The asset is a named image titled
`Agentworks AGW rocket with twin layered flames`. The `aria-hidden` `#mission-lander` references
that source with same-origin external uses: `#mission-mark` uses
`{{SITE_BASE}}assets/agw-rocket.svg#agw-mark`; `#mission-left-engine` and `#mission-right-engine`
use their corresponding `agw-engine-*` fragments.

The body color is `#292b30`. Each engine preserves, from outside to inside, `#d94a1e`, `#ff7a00`,
and `#ffe09a`. CSS transforms only the two `#mission-*-engine` use elements, never their nested
temperature layers.

### 3.2 Favicon projection

`agw-favicon.svg` uses `viewBox="0 0 240 425"` and contains only `#agw-mark` plus its three selected
letter paths. Their complete presentation attributes match the canonical elements in
`agw-rocket.svg`. The projection contains no plume or engine IDs, flame colors, script, style,
animation, image, use element, external reference, intrinsic width, or intrinsic height. Every page
references it through one site-base-aware `rel="icon" type="image/svg+xml"` link.

### 3.3 Lander reference point

The physics pose `(x, y, angle)` refers to the midpoint between the W's two lowest solid points at
asset coordinate `(120, 415)`. The inline scene applies transforms in this order:

```text
translate(worldX * 10, 548 - worldY * 10)
rotate(angleDeg)
scale(0.16)
translate(-120, -415)
```

The stable world group supplies the camera translation described in section 6. Positive world `x` is
right, positive world `y` is up, and positive `angle` is clockwise, matching SVG rotation. The
collision feet are model points `(-1.6 m, 0)` and `(1.6 m, 0)` relative to the reference point. The
lander width is therefore exactly `3.2 m`. Its solid hull is the rectangle from `(-1.6,0)` to
`(1.6,6.5)` transformed by its pose. Plumes are visual only and do not enlarge the hull.

Pure `transformLocalPoint(pose,lx,ly)` returns `worldX=x+lx*cos(a)+ly*sin(a)` and
`worldY=y-lx*sin(a)+ly*cos(a)` for clockwise degrees. Feet, hull, render, bay, and collision use the
same export.

## 4. Shared DOM and no-JavaScript contract

The Lander and 404 shells each render the shared `header`, `main`, and `footer` landmarks. The 404
breadcrumb is its sole visible route-home action. One reviewed `lander-game.html` fragment owns this
stable subtree and is rendered byte-equivalently into both shells after site-base substitution:

```text
section#lander-game[aria-label="Lunar deployment scene"]
  div#lander-scene-shell[tabindex="-1"]
    svg#lander-scene[viewBox="0 0 1000 640"]
      title#lander-scene-title
      desc#lander-scene-description
      rect#scene-sky
      path#scene-stars
      g#lander-world[aria-hidden="true"]
        g#terrain-layer
        g#site-layer
        g#debris-layer
        g#mission-lander
        g#mission-agent
      g#crash-flash[aria-hidden="true"]
      g#next-site-cue[aria-hidden="true"]
    button#lander-start[type="button"][hidden][disabled]
  p#lander-fuel[hidden]
    span#lander-fuel-label "Fuel reserve"
    output#lander-fuel-value[aria-labelledby="lander-fuel-label"]
  span#lander-target-direction.visually-hidden[hidden] "Next site is to the right."
  p#lander-controls[hidden]
  div#lander-actions[hidden]
    button#lander-exit[type="button"] "Exit mission"
    button#lander-restart[type="button"][hidden] "Restart mission"
  p#lander-status[role="status"][aria-live="polite"][aria-atomic="true"]
```

The template contains the complete static first terrain window, site 0, lander, gas can, and dark
NOC inside the listed layers. Enhancement reconciles those same nodes rather than keeping a hidden
second world. Generated terrain paths use `.terrain-chunk[data-chunk-index]`. Each retained site is
one `.lander-site[data-site-id][data-can="present|collected"][data-power="off|on"]` containing, in
order, `.landing-platform`, `.platform-supports`, `.gas-can`, `.noc-building`, `.noc-battery`, and
`.noc-antenna`. Reconciliation keys by the integer data value and replaces no stable outer layer.
Decorative world descendants remain `aria-hidden`.

The control copy reads exactly:

```text
Thrust: Space or Up. Turn: Left/H or Right/L. Tap or hold to thrust; drag to turn. R restarts after a crash. Escape exits.
```

Preflight hides fuel, controls, actions, direction cue, crash presentation, and all instructions.
Initialization is transactional. Before mutation, clone `#lander-game` as the pristine recovery
snapshot; build the model and controller in locals; and register every listener, media query,
animation frame, timer, and pointer-capture cleanup in one LIFO teardown registry. Render preflight,
then reveal and enable Start as the final operation before publishing the controller reference. If
any operation throws, run that same registry in reverse while catching each cleanup so later entries
still run, replace any partially changed game root with the snapshot, leave the controller reference
null, and report only to the console. The restored Start is hidden and disabled, static SVG
naming/ARIA is exact, and no listener, model, timer, capture, or mission attribute survives.
`destroy()` consumes the same idempotent registry. Absent JavaScript also preserves this static
recovery. START reveals fuel, controls, and actions, with Exit enabled and Restart hidden. Only
`failed` reveals and enables Restart. The live fuel output uses `fuel.toFixed(1)` for display but
retains the unrounded number in the model. A status update does not redundantly announce fuel every
frame; the named output changes only when its displayed tenth changes.

On start, the controller hides and disables `#lander-start`, gives `#lander-scene-shell`
`tabindex="0"`, `role="application"`, `aria-label="Lunar deployment game"`, and
`aria-describedby="lander-controls lander-fuel lander-target-direction lander-status"`, then focuses
it with `preventScroll: true`. Exit removes active attributes, restores `tabindex="-1"`, reveals and
enables Start, hides active chrome, and focuses Start without scrolling. Restart hides its button,
focuses the active shell without scrolling, and dispatches the same RESTART event as `r`.

Before activation the SVG is a named image whose description mentions the hovering lander, varied
lunar surface, elevated platform, gas can, and dark NOC, but no controls. While the shell is an
application, the SVG is `aria-hidden`; the live status and named fuel output convey changes. With
JavaScript unavailable, Start and all active chrome remain hidden while the named static scene, page
heading, 404 explanation, and breadcrumb remain useful.

The transparent start target covers the full preflight lander with at least 44 CSS pixels in each
dimension. At zero angle the complete asset occupies scene `x=[285.92,314.08]` and
`y=[163.2,243.36]`; center the target at `(30%,31.7625%)` and size it `max(44px,2.816%)` by
`max(44px,12.525%)`. Only `:focus-visible` draws its three-pixel outline and two-pixel offset.

## 5. Seed and deterministic world generation

### 5.1 Run seed lifecycle

World values are unsigned 32-bit integers. `lander-world.js` exports pure `normalizeSeed(value)`,
`mixUint32(value)`, and `sampleUnit(seed, stream, index)`. `normalizeSeed` converts with `>>>0` and
maps zero to `0x6d2b79f5`, so every run has one nonzero canonical seed. The exact mixer is:

```js
value = Math.imul(value ^ (value >>> 16), 0x7feb352d);
value = Math.imul(value ^ (value >>> 15), 0x846ca68b);
return (value ^ (value >>> 16)) >>> 0;
```

`sampleUnit` mixes `seed ^ Math.imul(stream, 0x9e3779b9) ^ Math.imul(index + 1, 0x85ebca6b)` and
divides by `2 ** 32`. Streams `1`, `2`, `3`, `4`, and `5` are terrain boundaries, terrain
orientation, template preference, corridor relief, and debris. Indexed samples make regeneration
independent of call order. There is no mutable PRNG cursor inside the world module.

The checked-in preflight scene uses `STATIC_WORLD_SEED=0x41475731`. Production START requests one
`Uint32Array(2)` from `crypto.getRandomValues` and passes both words through
`mixUint32(word0 ^ rotateLeft(word1,13))`. If Web Crypto is unavailable, the controller mixes
integer `Date.now()` and the integer microsecond portion of `performance.now()` once. It never uses
`Math.random`. The resulting nonzero seed replaces the static scene and exists only in the run
aggregate. Tests call `createRun({seed})`; ordinary START, Exit followed by START, and reload
acquire a fresh seed. Restart reuses the current run seed and checkpoint.

### 5.2 Terrain chunks

Constants are `CHUNK_WIDTH=20 m`, `TERRAIN_SAMPLE_SPACING=4 m`, and six vertices per chunk,
including both boundaries. Shared boundary height for integer boundary `b` is
`2 + 3 * sampleUnit(seed,1,b)`. For local vertex `k` in `[0,5]`, interpolate the two boundary
heights at `k/5`, then add the signed motif below. The sign is `+1` when
`sampleUnit(seed,2,chunkIndex) >= 0.5`, otherwise `-1`.

```text
k:       0     1      2     3      4    5
motif:   0   +1.2   -0.8  +1.0   -0.6  0   metres
```

Clamp final heights to `[0.75,7.5]`. Adjacent chunks share their boundary byte-for-byte. The motif
guarantees both materially rising and falling sampled edges before a platform replacement; the world
tests additionally prove that every retained representative window has one rise and one fall of at
least `0.35 m`, at least four nonzero slopes, and no three equal consecutive samples outside
platform spans.

`terrainHeightAt(seed,x)` linearly interpolates the enclosing sampled edge. A chunk path closes at
world `y=-10`, below the view. Terrain is collision geometry, not merely art.

### 5.3 Sites and constructive template selection

Site 0 has fixed platform center `x=36 m`; its top follows the same `0.8 m` terrain-clearance rule
as every other platform. The checked-in no-JavaScript scene is the exact site-0 descriptor for
`STATIC_WORLD_SEED`; START reconciles it to the fresh run seed before the first flight frame. Every
later target is a translation of one of section 10's nine constructive templates. The catalog maps
center delta to deck-height delta exactly:

```text
center delta:  78   81    84    87    90    93    96    99   102 m
deck delta:     0  +1.6  -0.8  +0.8  -1.6    0   -0.8  +0.8    0 m
```

For site index `i`, let `base=floor(8*sampleUnit(seed,3,i))`. Inspect the seven distinct nonfallback
slots `slot=1+((base+3*c)%8)` for `c=0..6`, then the `78 m` zero-delta template. Select the first
whose translated deck top is within the closed `TARGET_DECK_BAND=[1.55,8.3] m`. This is at most
eight constant-time eligibility checks, not route search. Every active deck is already in the band,
so the final zero-delta template is always eligible. The preference retains seeded distance and
elevation variety while continuity follows structurally for every seed, not from sampled seeds.

Each template carries collision-safe `clearanceKnots` relative to the origin center/deck. Relative
knot heights are at least `-0.65 m`, so every eligible translation has absolute cap at least
`0.9 m`. `lander-world.js` and the independent tool implement this exact construction separately:

```text
native(n):
  x = 4*n; q = floor(n/5); k = n-5*q
  if k == 0: raw = boundary(q)
  else: raw = lerp(boundary(q),boundary(q+1),k/5) + orientation(q)*motif[k]
  return clamp(raw,0.75,7.5)

originRight = originCenter+4.8; targetLeft = targetCenter-4.8
targetRight = targetCenter+4.8; padBase = targetTop-0.8
leave all existing vertices at x < originRight unchanged
begin the new ordered corridor with exactly (originRight,originTop-0.8)
for every integer n with originRight < 4*n < targetLeft, in increasing n:
  raw = native(n)
  cap = originTop + piecewiseLinear(clearanceKnots,4*n-originCenter)
  y = raw > cap ? max(0.75,cap-0.15*sampleUnit(seed,4,n>>>0)) : raw
  emit (4*n,y)
emit (targetLeft,padBase); discard every global sample in [targetLeft,targetRight]
emit (targetRight,padBase)
resume with the first native global sample strictly right of targetRight
join every adjacent emitted vertex with one straight segment
```

`boundary`, `orientation`, and `motif` are exactly section 5.2; equality at the cap gets no relief.
The explicit target-edge vertices make the entire replaced span flat with maximum `padBase`; the
single straight segments before `targetLeft` and after `targetRight` are the complete blend, with no
hidden easing or extra sample. Duplicate chunk-boundary vertices collapse to one identical value.
The model passes frozen geometry to pure
`instantiateTemplateSite(seed,siteIndex,originSite,templateGeometry)`; the world never imports or
selects the model catalog. Exact serialized vertices for every template at multiple translations and
the three pinned seeds catch predicate, index, clamp, relief, replacement, and blend drift.

A catalog/schema or replay mismatch is an invariant error: site state is left unchanged, the run
enters `generation-error`, and the live status becomes exactly
`Mission generation failed. Use Exit mission to start a new run.` No unreachable target appears,
Restart remains hidden and disabled, and `r` is ignored. Exit remains available and the next START
gets a fresh seed. This path defends against implementation corruption only; ordinary generation
cannot exhaust its template choices or enter an unbounded retry loop.

Each platform is exactly `3 * 3.2 = 9.6 m` long, centered at the site coordinate, `0.35 m` thick,
and `0.8 m` above its highest underlying collision-terrain point. Two `0.6 m`-wide pylons connect
its underside to native terrain. A centered `H` marking and outline make it read as a small elevated
helicopter pad. The platform replaces the terrain collision surface on its open horizontal span; its
top, ends, underside, and pylons are explicit solid geometry.

One gas can sits `3.0 m` right of platform center and does not collide. One NOC begins `2.0 m` right
of the platform edge. It is a single solid `7.0 m` by `7.2 m` building on a filled foundation up to
platform height, not separate modules. Its face contains one phone-battery outline, terminal, and
four fill bars. A solid `0.5 m`-wide, `3.2 m`-tall mast, antenna head, and two signal arcs rise from
the roof. Building, foundation, and mast collide; signal arcs do not.

## 6. Projection, camera, and bounded retention

The SVG uses `viewBox="0 0 1000 640"`, `preserveAspectRatio="xMidYMid meet"`, and
`width:min(100%,60rem)`, with no minimum width. It cannot cause page overflow at 320 CSS pixels or
400 percent zoom. Horizontal scale is `10 scene units/m`; vertical projection is
`sceneY=548-worldY*10`.

The controller computes `cameraLeft=max(0,renderedPose.x-35)` directly from the current immutable
model result and writes `--camera-x=-cameraLeft*10px` on the game root. CSS applies one transform on
`#lander-world`:

```text
transform: translate(var(--camera-x), 0)
```

All terrain, sites, lander, agent, and debris retain absolute world-derived scene coordinates inside
that group. The fixed sky, stars, crash flash overlay, and next-site cue remain outside it. The
camera does not rewrite child coordinates per frame. It begins moving only after the lander passes
`x=35`, keeps the reference point at scene `x=350`, and may move backward with the vehicle. Contact,
service, crash, and checkpoint restoration use their frozen or restored pose, with no monotonic
furthest-X value and no controller camera cache.

The visible interval is `[cameraLeft,cameraLeft+100]`. Retain chunks intersecting the interval plus
`40 m` on each side, at most ten chunks. Retain the active checkpoint site, target site, and at most
one immediately preceding powered site, at most three sites. When a window key changes, reconcile
the fixed layers once; ordinary frames update transforms and attributes only. The run retains no
discarded terrain or site history beyond `completedSites` and the latest checkpoint snapshot.

Hard runtime ceilings are ten terrain paths, three site groups, eight debris fragments, 80 children
under `#lander-world`, 64 queued input records, one pointer, one animation frame, and one pulse
timer. When enqueueing would create record 65, discard all queued edges, sample the controller's
complete physical keyboard and pointer state, and enqueue exactly one `INPUT_SNAPSHOT` for the next
integer simulation-step boundary. The snapshot contains the held physical codes plus pointer-active,
pointer ID, anchor/current X, and pulse-deadline timestamp needed by section 11's mixer.
Intermediate edges are deliberately lost; subsequent edges append after that record. This is
deterministic degradation, not an ordering-preservation claim. A 100-site browser witness must keep
these counts constant, show no increasing event-listener count, and keep active-game frame work
below 4 ms at the 95th percentile on the pre-merge Chromium machine. Direct template selection,
corridor construction, and exactly two proof replays together must finish below 25 ms at the 95th
percentile and 50 ms maximum over the same witness; record actual results rather than weakening the
ceiling.

The right-edge cue is a fixed `44 by 44` scene-unit target at `(932,280)`. It contains a solid
right-pointing arrow, not just motion. It is visible only after a target and its proof exist and
`target.platformLeft > cameraLeft+100`; it hides on equality or once the target enters view. At a
settled contact, the pose can be at most `3.2 m` right of platform center. Even the `78 m` template
puts the next platform's left edge at `center+73.2 m`, at least `5.0 m` beyond that contact-time
viewport right edge (`pose.x+65 m`). Near the origin, the fixed viewport gives a larger gap. Thus
the complete target site's leftmost extent is to the right of the viewport for every contact pose,
including a landing after arbitrary overshoot and return; the camera cannot inherit the overshoot.
The controller reveals the visually hidden `#lander-target-direction` on the same predicate, so
target direction is available without seeing animation or SVG.

## 7. Model shape, mission states, and checkpoint

### 7.1 Sole mutable run aggregate

`createRun({seed,reducedMotion=false})` returns a new aggregate with this conceptual shape:

```text
state, seed, missionSeconds, completedSites, awardRatio
pose, commanded, fuel
generatorCursor, retainedChunks, retainedSites
activeSiteId, targetSiteId, targetRouteProof
touchdownPose, sequenceSeconds, agent, nocStage
checkpoint, failureCause, crashOrdinal, crash
```

The object is replaced by pure model transitions; nested collections are copied on change. World
descriptors returned by `lander-world.js` are immutable values. Fuel is an unbounded nonnegative
JavaScript number measured in engine-seconds. There is no capacity, clamp, or award replacement:
unused fuel always carries forward.

Preflight uses the checked-in site 0 and initial pose but no active run seed or visible fuel. START
creates site 0 as target, fuel `30`, `completedSites=0`, `awardRatio=3`, and the initial approach:
`(x,y,vx,vy,angle,angularVelocity)=(30,32,0.8,-0.4,0,0)`.

### 7.2 State machine

The legal mission transitions are:

| From        | Event                                   | To                 |
| ----------- | --------------------------------------- | ------------------ |
| `preflight` | `START`                                 | `flying`           |
| `flying`    | safe target-platform contact            | `landed`           |
| `flying`    | unsafe contact or leg bound             | `crashing`         |
| `landed`    | `LANDING_SETTLED`                       | `deploying`        |
| `deploying` | `AGENT_ENTERED`                         | `powering`         |
| `powering`  | `NOC_POWERED`                           | `launching`        |
| `launching` | `LAUNCH_COMPLETE`                       | `flying`           |
| `crashing`  | `CRASH_COMPLETE`                        | `failed`           |
| `failed`    | `RESTART` with checkpoint               | `launching`        |
| `failed`    | `RESTART` before first checkpoint       | `flying`           |
| `flying`    | catalog/proof invariant error           | `generation-error` |
| Any active  | `EXIT`                                  | `preflight`        |
| `flying`    | unsafe contact with reduced motion      | `failed`           |
| `flying`    | safe target contact with reduced motion | `launching`        |

The reduced-motion safe-contact transition atomically applies the complete `landed`, `deploying`,
and `powering` state result, then begins the mechanical launch. There is no `succeeded` or terminal
deployment state.

Safe contact performs one indivisible service preparation before `landed` renders: generate and
prove the next site, mark the contacted can collected, add its award, increment `completedSites`,
advance `awardRatio` exactly once, and freeze upright at the deck. If next-site generation hits its
invariant error, none of these mutations commit and section 5.3's distinct non-restartable error
result applies. Normal timing is:

1. `landed`, 300 ms: settle, open the G bay, and show the collected-can/fuel result.
2. `deploying`, 1,800 ms: the agent descends for 300 ms, crosses to the NOC entry by 1,650 ms, and
   is hidden inside by 1,800 ms.
3. `powering`, 1,000 ms: battery fill bars become permanent at 200, 400, 600, and 800 ms; antenna
   head and static signal arcs activate at 1,000 ms.
4. At `NOC_POWERED`, write status `Agent deployed. Mission continues.`, mark the NOC powered, and
   create the checkpoint. Enter `launching` immediately afterward.
5. `launching`, 750 ms: command both engines at `0.72`, consume actual reserve through the ordinary
   fuel path, integrate the same immutable physics, ignore player input, and retain collision
   against platform sides, pylons, and the building. The starting deck-top contact is ignored only
   while velocity is upward and until both feet clear it by `0.05 m`. On completion, return control
   in `flying`, carrying the launch result across that transition without a reset.

Reduced motion skips the 3,100 ms service presentation, applies can collection, award, agent entry,
full battery, powered antenna, and checkpoint in one transition, then performs the same fuel-burning
launch at fixed simulation steps without CSS interpolation.

### 7.3 Immutable checkpoint

The model deep-copies and freezes this exact checkpoint after power and before launch:

```text
seed
completedSites
awardRatio = already advanced ratio for the next site's award
generatorCursor
pose.x = clamp(raw contact x, platformLeft + 1.6, platformRight - 1.6)
pose.y = platform top
pose.vx = pose.vy = pose.angle = pose.angularVelocity = 0
fuel = exact post-award reserve
activeSiteId
targetSiteId
targetRouteProof
retainedChunks = ordered chunk indexes
retainedSites = ordered descriptors with canCollected and powered flags
```

It excludes controller clocks, input, pointer, camera, animation progress, debris, status text, the
pre-crash flight pose, and `crashOrdinal`. The ordinal is run-lifetime presentation bookkeeping and
survives checkpoint restore; it cannot affect physics, world, fuel, or awards. RESTART clears the
other excluded values, restores a fresh checkpoint copy, keeps the current run seed, and enters
`launching`, so the restored vehicle visibly begins on the last powered pad and pays the same
automatic-launch fuel again. Repeated restarts restore exactly the same post-award fuel; they never
recollect the can, add the award, advance `awardRatio`, increment progress, or repower the NOC.
Before the first powered site, RESTART recreates the initial approach with the same run seed,
initial `30` fuel, and ratio `3`. Exit or ordinary reload discards the checkpoint and gets a fresh
seed and zero crash ordinal.

## 8. Physics, fuel, and fixed-step clock

### 8.1 Constants and integration

Constants are named exports and are not configurable at runtime:

```js
export const STEP_SECONDS = 1 / 120;
export const MAX_FRAME_SECONDS = 0.1;
export const MAX_CATCH_UP_STEPS = 12;
export const GRAVITY = 3.0;
export const ENGINE_ACCELERATION = 8.4;
export const TORQUE_ACCELERATION = 70.0;
export const FUEL_FLOW = 1.0;
export const MAX_PLAYABLE_Y = 56.0;
export const MAX_LANDING_HORIZONTAL_SPEED = 1.4;
export const MAX_LANDING_DESCENT_SPEED = 2.2;
export const MAX_LANDING_ANGLE = 8.0;
export const MAX_LANDING_ANGULAR_SPEED = 12.0;
```

Fuel burn is `FUEL_FLOW*(left+right)*STEP_SECONDS`. If reserve is smaller, scale both engines by
`fuel/requestedBurn`, exhausting it without favoring an engine. Effective engines drive both physics
and plumes. Zero fuel means zero thrust. Fuel has no mass effect.

Each step applies queued input through the step end, resolves effective thrust, stores the previous
pose, then uses the pre-step angle and semi-implicit Euler:

```text
total = ENGINE_ACCELERATION * (left + right)
ax = total * sin(angle)
ay = total * cos(angle) - GRAVITY
angularAcceleration = TORQUE_ACCELERATION * (left - right)
vx += ax * dt; vy += ay * dt; angularVelocity += angularAcceleration * dt
x += vx * dt; y += vy * dt; angle = normalizeDegrees(angle + angularVelocity * dt)
```

There is no drag, damping, bounce, random force, variable mass, position rounding, or doubled
gravity. Player and automatic-launch thrust share `effectiveThrust` and `integrateStep`.

### 8.2 Input mixing and scheduler

Digital and pointer mappings remain:

```text
collective = 0.72 for Space or Up, otherwise 0
leftBias = 0.45 for Left or H, otherwise 0
rightBias = 0.45 for Right or L, otherwise 0
keyboardLeft = clamp(collective + rightBias, 0, 1)
keyboardRight = clamp(collective + leftBias, 0, 1)
final engine = max(keyboard engine, pointer engine)
```

The model retains the one immutable scheduler exported by `lander-model.js`. The first callback
renders without stepping. A frame delta less than zero or greater than `0.1 s` discards the gap,
clears active input, and advances no mission time. Otherwise, run at most 12 fixed steps and retain
the substep residue. Timestamp ties use enqueue sequence; step-end comparison uses `1e-9 ms`.
`INPUT_SNAPSHOT` atomically replaces every modeled physical control at its assigned next-step
boundary. The overflow witness floods 65 alternating edges before a step, observes one snapshot plus
later edges, and produces the same state at 30, 60, and 120 Hz.

When hidden, cancel the animation frame, pointer/tap timer, key state, and active CSS animation; set
`data-paused="true"`; and retain no accumulator. On visibility, the first frame resets time without
stepping. Hidden time advances neither physics, service, crash, cue, arrow blink, nor mission time.

## 9. Swept collision and landing classification

Collision uses previous and next poses from every physics step. Broad phase includes nearby terrain,
the target platform and its `0.6 m` pylons, every NOC/foundation/solid `0.5 m` mast, and non-target
platforms. Interpolate position linearly and angle by the shortest arc. Set
`COLLISION_MARGIN=0.02 m`, hull radius `R=hypot(1.6,6.5)`, and
`travel=hypot(dx,dy)+R*abs(deltaAngleRadians)`. Use `N=ceil(travel/COLLISION_MARGIN)` equal-time
intervals, at least one and at most 64; a larger N is unsafe `overspeed`.

At every interval endpoint, test the hull against each unsafe feature's closed Euclidean Minkowski
expansion by `COLLISION_MARGIN`, implemented as closed polygon/segment distance `<=` the margin.
Every hull point moves at most the interval's translation plus `R` times its angular travel, which
is at most the margin. Therefore any real crossing, equality touch, or tangential graze between
samples must intersect the closed expansion at an adjacent endpoint. Take the first clear/hit
interval and bisect its expanded-distance predicate 12 times. This can stop at most `0.02 m` before
visible unsafe geometry but can never render through it; equality is a hit. Feature width is no
longer the proof.

Treat the target platform's top face separately so its upward normal is never margin-expanded into a
valid approach. For each sample interval, recursively test the conservative swept-hull enclosure
(the union of endpoint hull boxes expanded by that interval's motion bound) against the closed top
segment, left half before right. Discard a subinterval only when those closed sets are disjoint. An
endpoint penetration brackets a true top crossing and is bisected 12 times against unexpanded
geometry. A tangential interval still unresolved after 20 subdivisions or `1e-7 s` is conservatively
unsafe `grazing`; it cannot disappear through floating-point inequality. Platform ends, underside,
and pylons remain margin-expanded unsafe geometry. Only a bracketed true top crossing proceeds to
the safe-envelope test below, preserving ordinary pad landings without an early top hit.

Classify the earliest contact, with equal-time precedence: building or mast, non-top platform
surface, terrain, then target platform top. The target top is safe only when both transformed feet
are on its closed `9.6 m` span, neither hull side intersects an end, `vy<=0`, and these inclusive
limits hold at contact:

```text
abs(vy) <= 2.2
abs(vx) <= 1.4
abs(normalizeDegrees(angle)) <= 8.0
abs(angularVelocity) <= 12.0
```

After classifying the raw angled contact, settle upright: clamp reference `x` to
`[platformLeft+1.6,platformRight-1.6]`, set reference `y` to the platform top, set angle to zero,
and set both linear velocities and angular velocity to zero. This makes both transformed feet remain
on the closed deck span and supplies the exact later checkpoint pose. Gas-can art and antenna signal
arcs do not collide. Contact with a consumed/powered site's platform during a later leg is unsafe;
only the current target can complete a leg.

Per-leg bounds are `x >= checkpointCenter-45`, `x <= targetCenter+65`, and `y <= 56`. Before the
first checkpoint, use `x>=-5`. Crossing a bound is unsafe only when no earlier swept collision
exists. Terrain generation covers the whole bound corridor, so a missing collision node cannot turn
into a fall through the world.

## 10. Reference route proof and progressive award

### 10.1 Checked-in constructive catalog

`lander-model.js` owns an immutable `REFERENCE_TEMPLATES` array keyed by the nine distance and deck
deltas in section 5.3. It is data, not a runtime planner. Every entry contains these literal values:

```text
templateId, centerDelta, deckDelta
runs = ordered [commandIndex, fixedStepCount] pairs
clearanceKnots = ordered [relativeX, maximumTerrainYRelativeToOriginDeck] pairs
demonstratedMinimum, scheduleDigest
success = contact step, pose, burn, and classification
smallerFailure = allowance, fuel-exhaustion step, pose, and burn
```

All run counts are positive integers, adjacent command indexes differ, total schedule length is at
most `24*120=2,880` fixed steps, and the first run is the automatic launch prefix `[1,90]`. The
schedule stops at its first target contact. Clearance knots include both corridor endpoints, have
strictly increasing relative X, and linearly define the upper terrain envelope used in section 5.3.
The stored successful contact satisfies every inclusive landing limit. The smaller allowance's
trajectory remains inside its envelope until it exhausts fuel before target contact; replay stops at
that exhaustion step, which is its exact checked-in failure witness.

The schedule digest starts at `2166136261`. For each `[commandIndex,stepCount]`, fold the command,
then the low and high step-count bytes, in that order, using
`digest=Math.imul(digest ^ value,16777619)>>>0`. The stored digest must equal that exact result.

The only catalog command table is the exact stable keyboard state set below. Each state is reachable
by holding the listed physical controls for an integer number of fixed steps; pointer interpolation,
synthetic engine values, and substep input changes are forbidden in a template.

| Index | Left engine | Right engine | Held controls            |
| ----- | ----------- | ------------ | ------------------------ |
| 0     | `0`         | `0`          | none                     |
| 1     | `0.72`      | `0.72`       | Space                    |
| 2     | `0`         | `0.45`       | Left or H                |
| 3     | `0.45`      | `0`          | Right or L               |
| 4     | `0.72`      | `1`          | Space + Left or H        |
| 5     | `1`         | `0.72`       | Space + Right or L       |
| 6     | `0.45`      | `0.45`       | Left/H + Right/L         |
| 7     | `1`         | `1`          | Space + Left/H + Right/L |

### 10.2 Independent derivation tool

Keep `website/tools/derive_lander_routes.mjs` permanently. It uses only Node built-ins and must not
import production or test code; runtime, model, and tests must not import it. Version
`agw-lander-route-deriver/v1` independently implements sections 5.3, 8, 9, and the reachable command
table. Its versioned per-template constructive recipes give command phase order and finite integer
step ranges; it exhausts at most `2,000,000` lexicographically ordered combinations per template and
chooses by `(burn,totalSteps,RLE lexicographic)`, failing rather than emitting an incomplete route.

The exact invocation is:

```text
node website/tools/derive_lander_routes.mjs \
  --geometry website/tests/fixtures/lander-route-geometry-v1.json \
  --output PATH [--verify PATH]
```

Unknown/missing flags exit 2; derivation or verification failure exits 1; success exits 0.
`--geometry` contains schema `agw-lander-route-geometry/v1` and the nine IDs, deltas, and literal
clearance knots. Output schema `agw-lander-route-derived/v1` contains `deriverVersion`,
`physicsDigest`, `geometryDigest`, the ordered route records from section 10.1, and `outputDigest`.
Canonical JSON recursively sorts object keys, preserves array order, uses `JSON.stringify` without
whitespace, and hashes UTF-8 bytes with lowercase SHA-256. `geometryDigest` hashes the complete
geometry object; `physicsDigest` hashes an object containing every named numeric constant in
sections 8-10 plus the eight command rows; `outputDigest` hashes the output object with only
`outputDigest` omitted. The file adds one unhashed trailing LF.

The reviewed output is `website/tests/fixtures/lander-route-derived-v1.json`. The production model
embeds byte-equivalent template/route arrays and the three literal digest strings. Tests project
those arrays back to the two schemas, compare canonical bytes with both fixtures, and recompute all
digests before replay. Thus the world and tool consume identical envelope values while independently
implementing corridor construction, physics, and collision. Intentional regeneration writes to a
temporary path, uses `--verify` against the checked fixture, reviews any mismatch, then deliberately
updates geometry, derived fixture, and production literals together. Ordinary tests only verify
checked data and never regenerate expectations. `website/README.md` will permanently teach this
workflow. Neither fixture nor tool enters the 12-file artifact.

Catalog tests replay every literal from an upright origin with `fuel=demonstratedMinimum`, using the
exact production fixed-step physics and the translated corridor. Each must land at its literal
success vector. A second replay with `fuel=demonstratedMinimum-FUEL_QUANTUM` must match its literal
fuel-exhaustion witness before target contact. Exhaustively validate all nine entries and command
indexes. This establishes a conservative demonstrated minimum at the catalog's fixed schedule and
fuel resolution; it makes no claim about a lower-fuel schedule or a global physical optimum.

At site creation, direct selection translates the chosen literal geometry and constructs a
provisional checkpoint identical to the future real checkpoint except for fuel and the proof being
formed. It includes seed, upright origin pose, completed count, next award ratio, generator cursor,
site IDs, retained descriptors, collected current can, and powered current NOC. For each of exactly
two defensive replays, replace only fuel: first with the literal demonstrated minimum, then with one
quantum less. Neither replay reads carried reserve or award ratio. The first must reproduce success
and the second its checked-in failure. Any mismatch takes the defensive `generation-error` path;
there is no runtime search, optimization, descent, retry, or alternate-template loop.

### 10.3 Demonstrated minimum and award

Set `FUEL_QUANTUM=0.05`; every catalog minimum is greater than one quantum and is an exact integer
multiple of it. Independent derivation sets it to
`ceil(exact successful schedule burn/FUEL_QUANTUM)*FUEL_QUANTUM`; the next lower quantum exhausts
before contact. Store the template ID, successful replay, smaller-failure replay, quantum, literal
schedule digest, and burn in `targetRouteProof`. The runtime always performs exactly those two
replays and no fuel scan. The proof outcome and demonstrated minimum are therefore independent of
carried excess and of the stored ratio.

Use the run's stored `awardRatio` for the current award, then advance it exactly once with this O(1)
pure function:

```js
export function nextAwardRatio(current) {
  const floor = 1 + Number.EPSILON;
  if (current <= floor) {
    return floor;
  }
  const raw = 1 + (current - 1) * 0.82;
  return Math.max(floor, Math.min(raw, current - Number.EPSILON));
}

const award = demonstratedMinimum * model.awardRatio;
const nextRatio = nextAwardRatio(model.awardRatio);
```

This recurrence is equivalent to `1+2*0.82**i` until finite precision would repeat or cross the
floor. The explicit `current-Number.EPSILON` branch forces strict decrease before the floor. The
result then stays constant, and therefore non-increasing, at `1+Number.EPSILON`; it is always
greater than one. START stores `3`, so the first award is exactly `3*minimum`. Add `award` to
existing fuel without rounding or a capacity clamp, store `nextRatio` before incrementing to the
next site, and display only the resulting reserve to one decimal place. No award path loops over
`completedSites`.

The contacted site's gas can is consumed only after the proof succeeds. The award is based on the
new target, so site 0's can funds leg 1. Initial fuel funds the approach to site 0. After the NOC is
powered, replace provisional trial fuel with `carriedFuelAtContact + award`, attach the proof, and
freeze the real checkpoint with `nextRatio`. Proofs are byte-identical across different carried
reserves or ratios because neither is a proof input. With the same current ratio, different carried
reserves also produce the same award; changing the ratio changes only the award, never the proof.
Automatic launch spends from real checkpoint fuel in actual play. Carried excess can therefore
compensate for a later flight that uses more than the reference route.

## 11. Input, focus, and lifecycle

Match Space/arrows by `code` and `h`, `l`, `r` by lowercase `key`. Preflight starts only for a
non-repeated, unmodified Space targeted at body, game, shell, or scene. Exclude anchors, buttons,
form controls, editable elements, and descendants. The accepted key starts and seeds held Space at
the same timestamp. Native Start activation starts without thrust.

Only `flying` accepts flight keys on the active shell path. Consume repeats but enqueue only first
edges. Track aliases by physical code. Release accepted keys even after focus moves. Escape exits
any active state only on the shell path. Unmodified `r` and native Restart work only in `failed`.
Window blur, shell focusout, hide, exit, restart, contact, and destroy clear held input.

In `flying`, primary pointer button 0 captures one pointer and commands equal `0.72`. Horizontal
travel uses:

```text
deadZone = max(10 px, scene width * 0.01)
fullBiasDistance = max(56 px, scene width * 0.18)
bias = sign(dx) * clamp((abs(dx)-deadZone)/(fullBiasDistance-deadZone),0,1)
pointerLeft = clamp(0.72 + 0.28*bias,0,1)
pointerRight = clamp(0.72 - 0.28*bias,0,1)
```

A release within 180 ms and 10 CSS pixels retains a minimum 140 ms equal-thrust pulse. Pointer up,
cancel, lost capture, stall discard, contact, blur, hide, exit, restart, and destroy use one
idempotent teardown. `touch-action:none` applies only to the active flying shell. Elsewhere,
scrolling, zoom, text selection, and links retain browser behavior. Flight input is ignored during
service, launch, crash, and failure.

`destroy()` cancels the frame, listeners, media-query listener, capture, pulse timer, active ARIA,
status, and thrust; it hides and disables dead actions while leaving static recovery markup intact.
Never intercept Tab or trap focus. The header and breadcrumb remain available in every state.

## 12. Plumes, direction cue, NOC, and reduced motion

`plumeForThrust(u)` returns `scaleY=0.08+0.92*u` and `opacity=0.25+0.75*u`. CSS independently
transforms each external engine use around `(82,401)` and `(158,401)`. Plumes affect no collision or
layout.

Scene tokens remain local and fixed: sky `#f5f2e8`, stars `#8a867c`, terrain `#d7d2c4`, outlines and
platform `#4b4e55`, NOC shell `#20232a`, inactive battery `#3b3f47`, active battery `#7de2c5`, gas
can and antenna `#d94a1e`, and helipad marking `#f5f2e8`. Shape, outline, fill progression, and the
solid `H` keep platform, fuel, battery, and direction meaning independent of color.

`#lander-game` is the sole carrier of mission-wide state. Keyed `.lander-site` groups are
projections of model-owned per-site state, not independent authorities. The controller writes:

- `data-mission-state` from section 7;
- `data-paused`, `data-cue`, `data-target-offscreen`, and `data-reduced-motion`;
- `data-can="present|collected"`, `data-power="off|on"`, and `data-noc-stage="0|1|2|3|4|5"` on each
  retained keyed site group;
- custom properties `--camera-x`, `--lander-x`, `--lander-y`, `--lander-angle`, independent plume
  scale/opacity, `--agent-x`, `--agent-y`, `--crash-x`, `--crash-y`, and `--crash-progress`.

The only CSS keyframes are `agw-preflight-cue` and `agw-target-cue`. Preflight runs three subtle
plume pulses over 2.4 seconds once per document load; reduced motion and START settle immediately.
The target arrow blinks at a 900 ms period only while its solid right-pointing shape is visible,
motion is allowed, and the document is active. Reduced motion leaves it continuously visible.
`data-paused="true"` pauses both keyframes.

Agent travel, battery stage, launch, and crash are model-time projections, not CSS completion
events. The battery outline is always visible. Stages 1 through 4 fill one bar each; stage 5 changes
the terminal, antenna head, and two static arcs to powered colors. Once powered, attributes remain
on that retained site. Reduced motion removes transitions and applies the service result atomically.

## 13. Vacuum crash

Unsafe contact increments `crashOrdinal` from zero, creates state `crashing`, zeros engines and
input, freezes the lander at contact, and stores exactly eight fragment descriptors for normal
motion. For zero-based fragment `j`, set
`key=mixUint32(Math.imul(siteId+1,0x85ebca6b) ^ Math.imul(crashOrdinal,0xc2b2ae35) ^ Math.imul(j+1,0x27d4eb2f))`.
Property `p` uses `u[p]=sampleUnit(seed,5,(key + Math.imul(p,0x9e3779b9)) >>> 0)`. Set
`vx=-8+16*u[0]`, `vy=2+9*u[1]`, angular velocity `=-240+480*u[2]`, and color to
`[#292b30,#d94a1e,#ff7a00,#ffe09a][floor(4*u[3])]`. Initial points use transformed hull corner
`j%4`.

Normal-motion crash duration is exactly 600 ms. `#crash-flash` stays outside the translated world
group. At impact the controller pins its scene coordinates to `--crash-x=(contactX-cameraLeft)*10px`
and `--crash-y=(548-contactY*10)px`. The compact ellipse expands only within the SVG from 0 to `1.4`
lander widths and is visible for the first 140 ms. Debris stays inside `#lander-world` in absolute
world coordinates. A fragment follows `x=x0+vx*t`, `y=y0+vy*t-0.5*GRAVITY*t*t`, with its stored
rotation. Clip it to the scene; remove all flash and fragment nodes at 600 ms; enter `failed`;
reveal Restart; and set status `Landing unsuccessful. Press R to restart or Escape to exit.`

There is no smoke, dust cloud, atmospheric shock wave, sustained flame, sound, vibration, camera
shake, page movement, or layout change. Reduced motion still increments the ordinal, skips flash and
fragment travel, creates exactly zero debris nodes, and enters the same final failed state in the
contact task.

## 14. Deterministic vectors

Numerical physics tests use tolerance `1e-10`; strings, integers, states, seed values, DOM order,
and serialized world descriptors are exact. Every schedule includes an explicit final callback.

| Vector                | Input                                                                  | Expected result                                                                                                   |
| --------------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Gravity, 120 steps    | `(10,30,0,0)`, zero angle/engines, fuel 30                             | `x=10`, `y=28.4875`, `vx=0`, `vy=-3`, fuel `30`                                                                   |
| Collective, 120 steps | Same pose, engines `(0.72,0.72)`                                       | `y=34.5854`, `vy=9.096`, angle/x unchanged, fuel `28.56`                                                          |
| One right-engine step | Same pose, engines `(0,1)`                                             | `y=30.000375`, `vy=0.045`, angular velocity `-0.583333333333`, angle `-0.00486111111111`, fuel `29.9916666666667` |
| Exhaustion            | Fuel `0.005`, one step, engines `(1,1)`                                | Effective engines `(0.3,0.3)`, fuel exactly `0`                                                                   |
| Plumes                | `u=0,0.5,1`                                                            | scales `0.08,0.54,1`; opacities `0.25,0.625,1`                                                                    |
| First site            | Any normalized seed                                                    | ID `0`, platform center `36`, width `9.6`, top `native maximum+0.8`, NOC count `1`, can present                   |
| Ratio                 | Start at `3`; apply `nextAwardRatio` successively                      | `3`, `2.64`, `2.3448`, then strict decrease to constant `1+Number.EPSILON`; O(1) per call                         |
| Safe inclusive edge   | Target top; `vx=1.4,vy=-2.2,angle=-8,omega=12`                         | safe contact                                                                                                      |
| Unsafe epsilon        | Any one safe magnitude increased by `1e-9`                             | unsafe contact                                                                                                    |
| Swept unsafe equality | Hull only grazes a terrain/pylon/mast edge between step endpoints      | closed 0.02 m expansion detects it; no visual tunneling                                                           |
| Target-top separation | Safe descent over deck center; then a separate exact tangential graze  | descent uses true top crossing and can be safe; unresolved graze is unsafe                                        |
| Frame equivalence     | Initial approach, no input, callbacks to 1,000 ms at 30, 60, and 120Hz | 120 steps; `x=30.8`, `y=30.0875`, `vx=0.8`, `vy=-3.4`, fuel `30`                                                  |
| Checkpoint replay     | Award, launch, crash, RESTART twice                                    | identical post-award fuel/site flags/next ratio; no can, award, ratio, or progress duplication                    |
| Catalog quantum       | Every checked-in reference template                                    | allowance `minimum` matches literal safe contact; `minimum-0.05` matches literal failure                          |
| Input overflow        | 65 alternating edges before one step at 30, 60, and 120 Hz             | queue becomes one next-step physical-state snapshot; all frame schedules produce the same result                  |
| Long run              | 100 successful deterministic sites                                     | fixed work per ratio advance; bounded nodes/edges; reserve equals initial plus awards minus all burn              |

World tests pin complete JSON descriptors and route-proof digests for seeds `1`, `0x12345678`, and
`0xffffffff`. The independent world fixtures begin with these exact values:

| Seed         | `mixUint32(seed)` | Chunk 0 heights                                                                             | Site 0 top           | Leg-1 template preference  |
| ------------ | ----------------- | ------------------------------------------------------------------------------------------- | -------------------- | -------------------------- |
| `1`          | `1753845952`      | `3.632365759695,2.237045118399,4.041724477103,2.046403835807,3.451083194511,2.655762553215` | `4.3452401980757713` | `102,87,96,81,90,99,84,78` |
| `0x12345678` | `4125564054`      | `2.894239616115,4.222849254729,2.351458893344,4.280068531958,2.808678170573,3.537287809188` | `4.6608121663331987` | `99,84,93,102,87,96,81,78` |
| `0xffffffff` | `1734902346`      | `2.975416276604,1.709050793713,3.642685310822,1.776319827931,3.309954345040,2.643588862149` | `4.3304941696580501` | `87,96,81,90,99,84,93,78`  |

The static scene seed's site-0 top is exactly `5.119569691829383`. The implementation commit also
records literal template schedules, success/failure vectors, envelopes, instantiated-site
descriptors, and proof digests from section 10's independent derivation; tests must not generate
expected values by calling the function under test. For each seed, tests cover at least three sites,
terrain diversity, preference and eligibility order, guaranteed zero-delta selection, contact-time
offscreen placement, both proof replays, exact award, and rolling-window eviction.

## 15. Verification matrix

The exact local demo remains
`python3 website/build.py --repo-root . --output /tmp/agentworks-site-demo --site-base /`, followed
by `python3 -m http.server --directory /tmp/agentworks-site-demo 8000`. Game work opens `/lander/`;
fallback acceptance opens `/404.html`.

The complete artifact contains exactly these 12 files:

```text
index.html
manifesto/index.html
security/index.html
lander/index.html
404.html
assets/agw-favicon.svg
assets/agw-rocket.svg
static/site.css
static/lander.css
static/lander-world.js
static/lander-model.js
static/lander-game.js
```

| Layer                                                                   | Required coverage                                                                                                                                                                                                                                                                                                                                            |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `node --test website/tests/lander-world.test.mjs`                       | Mixer/seeds; exact terrain/site vectors; shared boundaries/diversity; every corridor pseudocode branch, global index, cap equality, relief/clamp, target replacement, boundary blend, chunk deduplication; template eligibility/zero-delta guarantee; exact geometry-fixture digest; pad/NOC/can; window/retention/offscreen/immutability                    |
| `node --test website/tests/lander-model.test.mjs`                       | State/events; 8.4 physics, fuel/carry, scheduler/overflow; closed-margin crossing/equality/tangency, overspeed, exact top safe crossing and grazing failure; literal catalog/reachable commands; checked derivation digests, success/failure and exactly two replays; ratio/checkpoint/generation error/launch/debris/ordinal                                |
| Derivation CLI fixture verification                                     | Run section 10.2's command to a temporary output with `--verify website/tests/fixtures/lander-route-derived-v1.json`; exact version/schema/digests, bounded recipe exhaustion, deterministic bytes, and nonzero mismatch/usage exits; import-closure tests prove the tool and production/test modules do not import one another                              |
| `python -m unittest discover -s website/tests -p 'test_*.py'`           | Exact 12-file artifacts at both bases, excluding tools/fixtures; focused validation helper; local module closure; byte-equivalent DOM; transactional-init structure and hidden/disabled static Start; fuel/actions; local SVG/CSS; forbidden network, storage, audio, canvas, service-worker, navigation, cookie, and uncontrolled randomness                |
| Manual Chrome and Edge pre-merge; Firefox and Safari/WebKit post-launch | Start/focus; injected exception after each initialization stage restores exact static DOM with no listener/timer/capture; Space/arrows/vi/touch; three sites; pad/NOC/can; power/arrow; carry/empty fuel; all collision/crash surfaces; checkpoint/Exit/hidden pause; request log proving zero game requests                                                 |
| Manual responsive and accessibility acceptance                          | 320 CSS pixels, 400 percent zoom, touch landscape, and wide viewport; no overflow or clipped actions; 44-pixel targets; logical focus; no trap; useful no-CSS/no-JS order; named fuel value; restrained live announcements; solid direction cue; static reduced-motion cue; silent decorative SVG; fixed-token 4.5:1 text and 3:1 necessary-graphic contrast |
| Performance and longevity witness                                       | 100-site deterministic run; ceilings in section 6 never exceeded; direct selection/corridor/two-replay timing recorded; hidden tab has no frame or mission progress; normal active frame p95 below 4 ms; teardown leaves no listener, timer, capture, frame, enabled dead action, or growing retained history                                                |

Mutation tests reject duplicated/moved shared markup, a second scheduler/controller/site authority,
game checks added to the near-limit validator, artifact count drift, pad-width drift, fuel caps, can
recollection, proof dependence on carried fuel, route proofs that omit launch, ratio recomputation
from `completedSites`, any runtime planner/search/fuel scan or third proof replay, a catalog command
outside the reachable table, production-derived expected fixtures, derivation-tool imports, corridor
algorithm/digest drift, open or unmarginated unsafe collision, margin-expanded target top, partial
initialization residue, retained-node growth, monotonic-furthest-X camera state, zero normal-motion
debris, animated-only direction, atmospheric crash effects, or a durable/network surface.

## 16. Traceability

| Requirement or decision                                                | Pinned by                            |
| ---------------------------------------------------------------------- | ------------------------------------ |
| R6, D5: selected custom mark, twin plumes, and favicon                 | Sections 2 and 3                     |
| R7, AC5, AC19: hidden shared 404/Lander game and byte-equivalent DOM   | Sections 4, 11, and 15               |
| R8, AC6: stronger physics, keyboard/vi/touch, independent plumes       | Sections 8, 11, 12, and 14           |
| R9, AC8: no-JS, in-memory lifecycle, pause, focus, reduced motion      | Sections 4, 5.1, 8.2, 11, 12, and 15 |
| R21, AC7: rolling terrain, exact elevated pad, NOC, can, repeated legs | Sections 5-7, 10.3, 12, and 15       |
| R22, AC22: seeded target, demonstrated minimum, ratio, carryover       | Sections 5, 7.3, 10, 14, and 15      |
| R22, AC23: offscreen target and motion-safe right cue                  | Sections 6, 8.2, 12, and 15          |
| R23, AC24: vacuum crash and exact checkpoint restart                   | Sections 7.3, 9, 13-15               |
| AC18: complete build only and exact local manifest                     | Sections 2 and 15                    |
| Phase 4G: focused modules, bounded work, docs, and browser evidence    | Sections 2, 6, 14, and 15            |

Implementation treats this LLD as temporary design input. Permanent source, tests, and
`website/README.md` stand on their own and do not link back to this SDD path.
