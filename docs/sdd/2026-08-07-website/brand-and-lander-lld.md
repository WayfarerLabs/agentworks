# LLD: AGW Brand and Continuous Lunar Deployment Lander

<!-- cspell:ignore focusout imul keyup pointerdown pointerup PRNG repower -->
<!-- cspell:ignore lerp Minkowski overspeed subinterval unhashed unmarginated -->
<!-- cspell:ignore substep unitless uint32 quantized quantization -->

- Status: Phase 4H tuning design pinned; implementation pending
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
NOC. **Commanded thrust** is the post-input, post-assist, post-fuel engine value shared by physics
and plumes. **Manual steer** is the normalized signed turn intent before angular assistance;
negative is left and positive is right. **Thrust-vector angle** is the manual-steer-derived
direction shared by both engine forces and both rendered plumes while effective thrust is nonzero;
it is zero at zero effective thrust. **Mission time** excludes hidden time. A **demonstrated
minimum** is the smallest fuel allowance, at the pinned fuel quantum, that completes one checked-in
constructive reference schedule. It is not a global mathematical optimum over all possible controls.

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
| `website/tests/fixtures/lander-route-derived-v2.json`  | Reviewed independent schedules and witness output               |
| `website/tests/lander-world.test.mjs`                  | Seeded world, window, site, and template vectors                |
| `website/tests/lander-model.test.mjs`                  | Scheduler, physics, mission, fuel, and checkpoint vectors       |
| `website/tests/test_lander_404.py`                     | Build, DOM, no-JS, and forbidden-surface checks                 |
| `website/tests/lander-browser-checklist.md`            | Package-free browser, performance, and accessibility acceptance |

The shipped artifact grows by exactly one file, `static/lander-world.js`. Tool and test files are
not in the artifact. The production module DAG is exact:

```text
lander-game.js  -> lander-model.js -> lander-world.js
       |--------------------------------^  read-only projection and seed helpers
```

`lander-game.js` imports the model API plus only the pure `cameraLeftForPose`, `terrainPath`,
`targetIsOffscreen`, and `mixUint32` helpers directly from `lander-world.js`. `lander-model.js`
imports pure world construction, retention, seed, and geometry exports. `lander-world.js` imports
neither production module, reads no DOM, clock, storage, or ambient randomness, and owns no mutable
singleton. No production module imports upward or sideways outside this DAG.

The model is the sole mutable run authority. One run aggregate owns physics, fuel, mission state,
seed, generator cursor, retained sites, active and target IDs, route proof, checkpoint, and crash
debris. The controller is the sole browser adapter and owns browser listeners, the animation frame,
focus, pointer capture, CSS projection, and entropy acquisition. Neither lower module accesses a
browser global. The controller must not keep a second site, fuel, checkpoint, or mission copy.

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
`.noc-antenna`. The existing single `.platform-supports` path contains the filled collision-riser
face and its stroked support/truss treatment; it adds no wrapper or decorative child. Reconciliation
keys by the integer data value and replaces no stable outer layer. Decorative world descendants
remain `aria-hidden`.

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
divides by `2 ** 32`. Streams `1`, `2`, `3`, `4`, and `5` are terrain boundaries, terrain motif-bank
selection, template preference, corridor relief, and debris. Indexed samples make regeneration
independent of call order. There is no mutable PRNG cursor inside the world module.

The checked-in preflight scene uses `STATIC_WORLD_SEED=0x41475731`. Production START requests one
`Uint32Array(2)` from `crypto.getRandomValues` and passes both words through
`mixUint32(word0 ^ rotateLeft(word1,13))`. If Web Crypto is unavailable, the controller mixes
integer `Date.now()` and the integer microsecond portion of `performance.now()` once. It never uses
`Math.random`. The resulting nonzero seed replaces the static scene and exists only in the run
aggregate. Tests call `createRun({seed})`; ordinary START, Exit followed by START, and reload
acquire a fresh seed. Restart reuses the current run seed and checkpoint.

### 5.2 Terrain chunks

Constants are `CHUNK_WIDTH=50 m`, `TERRAIN_SAMPLE_SPACING=10 m`, and six vertices per chunk,
including both boundaries. `lander-world.js` is the sole production owner of the exported chunk
width; retention, model bounds, and controller projection import it rather than repeating `50`.
Shared boundary height for integer boundary `b` is `1.5 + 4.5 * sampleUnit(seed,1,b)`. The exact
motif bank is:

```text
k:    0     1      2     3      4    5
M0:   0   +2.4   -1.5  +1.8   -1.1  0   metres
M1:   0   -2.1   -0.8  +2.2   +1.0  0   metres
M2:   0   +0.9   +2.5  +0.6   -1.9  0   metres
M3:   0   -1.4   +1.3  +2.4   -0.5  0   metres
```

For seed `seed`, choose one traversal of the bank for the complete world:

```text
offset = floor(4*sampleUnit(seed,2,0))
direction = sampleUnit(seed,2,1) < 0.5 ? 1 : 3
positiveModulo(value,4) = ((value % 4)+4)%4
motifIndex(chunkIndex) = positiveModulo(offset+direction*chunkIndex,4)
```

For local vertex `k` in `[0,5]`, interpolate the two boundary heights at `k/5`, add
`MOTIFS[motifIndex(chunkIndex)][k]`, then clamp the final height to `[0.5,7.5]`. There is no motif
orientation or sign step. Because `direction` is `1` or `3` modulo four, the selected indexes across
any four consecutive chunks are all different, including across negative chunk indexes. Adjacent
chunks share their boundary byte-for-byte because every motif begins and ends at zero. The bank
guarantees varied shapes while seeded pseudorandom boundary trends vary their absolute slopes
further. World tests prove the exact bank and traversal, four distinct indexes in every
representative four-chunk window, one rise and one fall of at least `0.5 m`, at least four nonzero
slopes, and no three equal consecutive samples outside site shelves. A single motif or repeated
motif selection fails even if its sampled heights happen to retain rises and falls.

`terrainHeightAt(seed,x)` linearly interpolates the enclosing sampled edge. A chunk path closes at
world `y=-10`, below the view. Terrain is collision geometry, not merely art.

### 5.3 Sites and constructive template selection

Site 0 has fixed platform center `x=36 m`. Its shelf begins at `platformLeft`, ends at
`platformRight+9 m`, and replaces native terrain at one height; its platform top is exactly `0.8 m`
above the maximum native height over that complete future shelf span. The clamp in section 5.2
therefore keeps its top inside `TARGET_DECK_BAND`. The checked-in no-JavaScript scene is the exact
site-0 descriptor for `STATIC_WORLD_SEED`; START reconciles it to the fresh run seed before the
first flight frame. Every later target is a translation of one of section 10's nine constructive
templates. The catalog maps center delta to deck-height delta exactly:

```text
center delta:  78   81    84    87    90    93    96    99   102 m
deck delta:     0  +1.6  -0.8  +0.8  -1.6    0   -0.8  +0.8    0 m
```

The site-0 band guarantee remains structural with the motif bank. The shelf contains `x=49.8`, which
is `98%` of the final sampled edge from `x=40` to the chunk boundary at `x=50`. Its clamped
endpoints are at least `0.5` and `1.5`, respectively, so native height there is at least
`lerp(0.5,1.5,0.98)=1.48`; site 0 top is therefore at least `2.28`. Every clamped shelf height is at
most `7.5`, so its top is at most `8.3`. Later selection still inspects only the translated deck
top, and the three zero-delta routes remain structurally eligible for every active deck in the band;
the motif bank cannot change that termination proof.

For site index `i`, let `base=floor(9*sampleUnit(seed,3,i))` and inspect all nine catalog slots in
the order `slot=(base+4*c)%9` for `c=0..8`. Four and nine are relatively prime, so this is one
complete seed-rotated permutation with no duplicate or omitted template. Select the first whose
translated deck top is within the closed `TARGET_DECK_BAND=[1.55,8.3] m`. This is at most nine
constant-time eligibility checks, not route search. The catalog contains zero-delta routes at `78`,
`93`, and `102 m`; every active deck is already in the band, so all three are eligible and every
permutation must reach one. The preference retains seeded distance and elevation variety while
continuity follows structurally for every seed, not from sampled seeds. No entry is a dead fallback
or a special retry path.

Each template carries collision-safe `clearanceKnots` relative to the origin center/deck. Relative
knot heights are at least `-0.65 m`, so every eligible translation has absolute cap at least
`0.9 m`. `lander-world.js` and the independent tool implement this exact construction separately:

```text
native(n):
  x = 10*n; q = floor(n/5); k = n-5*q
  if k == 0: raw = boundary(q)
  else: raw = lerp(boundary(q),boundary(q+1),k/5) + MOTIFS[motifIndex(q)][k]
  return clamp(raw,0.5,7.5)

originShelfRight = originCenter+4.8+9; targetShelfLeft = targetCenter-4.8
targetShelfRight = targetCenter+4.8+9
originPadBase = originTop-0.8; targetPadBase = targetTop-0.8
leave all existing vertices at x < originShelfRight unchanged
begin the new ordered corridor with exactly (originShelfRight,originPadBase)
for every integer n with originShelfRight < 10*n < targetShelfLeft, in increasing n:
  raw = native(n)
  cap = originTop + piecewiseLinear(clearanceKnots,10*n-originCenter)
  y = raw > cap ? max(0.5,cap-0.15*sampleUnit(seed,4,n>>>0)) : raw
  emit (10*n,y)
emit (targetShelfLeft,targetPadBase)
discard every global sample in [targetShelfLeft,targetShelfRight]
emit (targetShelfRight,targetPadBase)
resume with the first native global sample strictly right of targetShelfRight
join every adjacent emitted vertex with one straight segment
```

`boundary`, `MOTIFS`, and `motifIndex` are exactly section 5.2; equality at the cap gets no relief.
For site 0, discard native samples in its closed shelf span, emit the two shelf endpoints at its
`padBase`, and join each endpoint directly to the adjacent native sample outside the span. For later
sites, the explicit target shelf vertices make the platform, the `2 m` gap, and the complete `7 m`
NOC footprint one flat collision span. The single straight segments before `targetShelfLeft` and
after `targetShelfRight` are the complete deterministic blends, with no hidden easing or extra
sample. Duplicate chunk-boundary vertices collapse to one identical value. The model passes frozen
geometry to pure `instantiateTemplateSite(seed,siteIndex,originSite,templateGeometry)`; the world
never imports or selects the model catalog. Exact serialized vertices for every template at multiple
translations and the exact ordered seeds/translations in section 10.2 catch predicate, motif index,
clamp, relief, shelf replacement, NOC foundation, and both blend boundaries.

A catalog/schema or replay mismatch is an invariant error: site state is left unchanged, the run
enters `generation-error`, and the live status becomes exactly
`Mission generation failed. Use Exit mission to start a new run.` No unreachable target appears,
Restart remains hidden and disabled, and `r` is ignored. Exit remains available and the next START
gets a fresh seed. This path defends against implementation corruption only; ordinary generation
cannot exhaust its template choices or enter an unbounded retry loop.

Each platform is exactly `3 * 3.2 = 9.6 m` long, centered at the site coordinate, `0.35 m` thick,
and its top is exactly `0.8 m` above the shelf at `padBase=platformTop-0.8 m`. One solid riser spans
the full platform width from the deck underside at `platformTop-0.35 m` down to `padBase`; it is a
closed collision polygon, so no pale sky aperture exists below the deck. The existing single
`.platform-supports` path renders the identical outer rectangle with platform-outline fill plus
stroked inset diagonals that read as an intentional support face/truss. In scene coordinates, let
`L=platformLeft*10`, `R=platformRight*10`, `B=548-platformBottom*10`, and `S=548-padBase*10=B+4.5`.
Begin its `d` with the closed face `M L B H R V S H L Z`. The `96`-unit face has six exact `16`-unit
bays; for `i=0..5`, append both diagonals `M(L+16*i) B L(L+16*(i+1)) S` and
`M(L+16*i) S L(L+16*(i+1)) B`. The face fill is `#4b4e55`; its one-unit outline/diagonals are
`#292b30`. Those internal strokes are decorative parts of the same path and add no collision edge or
DOM node. The checked-in static path, dynamic path, and model polygon use the same four outer
coordinates. A centered `H` marking and outline keep the complete structure legible as a small
elevated helicopter pad. The deck top, ends, underside, solid riser, and shelf terrain are explicit
solid geometry.

One gas can sits `3.0 m` right of platform center and does not collide. One NOC begins `2.0 m` right
of the platform edge. Its foundation bottom is exactly the shared shelf `padBase`, never a native or
interpolated terrain sample. It is one solid `7.0 m`-wide shell whose roof remains `7.2 m` above
platform top, with the filled foundation and building projected as one shape rather than separate
modules. Its face contains one vertical phone-battery outline, terminal, and four fill bars. A solid
`0.5 m`-wide, `3.2 m`-tall mast, antenna head, and two signal arcs rise from the roof. Building,
foundation, and mast collide; signal arcs do not.

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
`40 m` on each side, at most five `50 m` chunks. Retain the active checkpoint site, target site, and
at most one immediately preceding powered site, at most three sites. When a window key changes,
reconcile the fixed layers once; ordinary frames update transforms and attributes only. The run
retains no discarded terrain or site history beyond `completedSites` and the latest checkpoint
snapshot.

Hard runtime ceilings are five terrain paths, three site groups, eight debris fragments, 80 children
under `#lander-world`, 64 queued input records, one pointer, one animation frame, and one pulse
timer. When enqueueing would create record 65, discard all queued edges, sample the controller's
complete physical keyboard and pointer state, and enqueue exactly one `INPUT_SNAPSHOT` for the next
integer simulation-step boundary. The snapshot contains the held physical codes plus pointer-active,
pointer ID, monotonically assigned pointer token, anchor/current X, completed pointer token, and
pulse-deadline timestamp needed by section 11's mixer. Intermediate edges are deliberately lost;
subsequent edges append after that record. This is deterministic degradation, not an
ordering-preservation claim. A 100-site browser witness must keep these counts constant, show no
increasing event-listener count, and keep active-game frame work below 4 ms at the 95th percentile
on the pre-merge Chromium machine. Direct template selection, corridor construction, and exactly two
proof replays together must finish below 25 ms at the 95th percentile and 50 ms maximum over the
same witness; record actual results rather than weakening the ceiling.

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

`commanded` is exactly `{left,right,vectorAngle}`: post-assist/post-fuel effective engines plus the
manual-steer-derived angle in degrees when their sum is nonzero. It is the sole renderer input for
plume length and direction; the controller never reconstructs steer from the assisted differential.
Whenever effective `left+right` is zero, including an idle or already-exhausted flight step,
`vectorAngle` is exactly zero even though that step's physics may retain its pre-assist manual `s`.
Every non-flight, input teardown, failure, and empty-fuel zero-force transition therefore sets all
three values to zero.

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
3. `powering`, 1,000 ms: the vertical battery fills from its bottom toward the antenna at 200, 400,
   600, and 800 ms, one permanent colored bar per boundary; terminal, antenna head, and static
   signal arcs activate at 1,000 ms.
4. At `NOC_POWERED`, write status `Agent deployed. Mission continues.`, mark the NOC powered, and
   create the checkpoint. Enter `launching` immediately afterward.
5. `launching`, 750 ms: command both engines at `0.72`, consume actual reserve through the ordinary
   fuel path, integrate the same immutable physics, ignore player input, and retain collision
   against platform sides, the solid riser, and the building. The starting deck-top contact is
   ignored only while velocity is upward and until both feet clear it by `0.05 m`. On completion,
   return control in `flying`, carrying the launch result across that transition without a reset.

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
pose.x = platform center
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
export const ENGINE_ACCELERATION = 9.0;
export const TORQUE_ACCELERATION = 80.0;
export const FUEL_FLOW = 1.0;
export const TURN_DIFFERENTIAL = 0.375;
export const TURNING_TOTAL = 1.2;
export const MAX_THRUST_VECTOR = 18.0;
export const ANGULAR_ASSIST_DIFFERENTIAL = 0.12;
export const ANGULAR_ASSIST_FULL_SPEED = 15.0;
export const MAX_PLAYABLE_Y = 56.0;
export const MAX_LANDING_HORIZONTAL_SPEED = 1.6;
export const MAX_LANDING_DESCENT_SPEED = 2.5;
export const MAX_LANDING_ANGLE = 10.0;
export const MAX_LANDING_ANGULAR_SPEED = 15.0;
```

Let the raw post-input request be `(rawLeft,rawRight)`, `T=rawLeft+rawRight`, and
`s=clamp((rawLeft-rawRight)/TURN_DIFFERENTIAL,-1,1)`. Capture `s` before angular assistance and fuel
scaling; it is the manual steer and therefore the thrust-vector direction. Negative `s` turns left.

Angular assistance is active control, never passive drag. It applies only when `s` is exactly zero
and `T>0`:

```text
rawAssist = ANGULAR_ASSIST_DIFFERENTIAL * clamp(-angularVelocity / ANGULAR_ASSIST_FULL_SPEED,-1,1)
differenceLimit = min(T,2-T)
dAuto = clamp(rawAssist,-differenceLimit,differenceLimit)
assistedLeft = (T+dAuto)/2
assistedRight = (T-dAuto)/2
```

For manual steer or `T=0`, `dAuto=0` and the assisted pair equals the raw pair. The limit keeps both
engines in `[0,1]` without changing their sum, so assistance has exactly the same fuel use and
forward-force magnitude as the neutral collective it redistributes. Its differential plume lengths
make the active correction visible. Manual steer disables it rather than fighting the player.
Engine-off flight retains both linear and angular velocity; crash fragments never receive assist.

Fuel burn is `FUEL_FLOW*(assistedLeft+assistedRight)*STEP_SECONDS`. If reserve is smaller, scale
both assisted engines by `fuel/requestedBurn`, exhausting it without favoring an engine. Effective
engines drive physics and plume length. Store `commanded.vectorAngle=MAX_THRUST_VECTOR*s` only when
the effective engine sum is nonzero; otherwise store zero. Zero fuel means zero thrust and cannot
leave an idle plume gimbaled. Fuel has no mass effect.

Each step applies queued input through the step end, resolves effective thrust, stores the previous
pose, then uses the pre-step angle and semi-implicit Euler:

```text
delta = radians(MAX_THRUST_VECTOR * s)
total = ENGINE_ACCELERATION * (left + right)
forceAngle = radians(angle) + delta
ax = total * sin(forceAngle)
ay = total * cos(forceAngle) - GRAVITY
angularAcceleration = TORQUE_ACCELERATION * (left - right)
vx += ax * dt; vy += ay * dt; angularVelocity += angularAcceleration * dt
x += vx * dt; y += vy * dt; angle = normalizeDegrees(angle + angularVelocity * dt)
```

Here `left` and `right` are the effective post-assist, post-fuel values, while `s` remains the
pre-assist manual intent. Both engine forces use the same `delta`; their sum therefore has a real
lateral component and a forward component reduced by `cos(delta)`. The post-fuel engine difference
drives torque, so partial exhaustion proportionally reduces both translation and rotation. There is
no environmental drag, damping, bounce, random force, variable mass, position rounding, or doubled
gravity. Coasting in vacuum preserves velocity and angular velocity exactly apart from gravity; only
the explicitly fueled neutral-collective assist arrests rotation. Player and automatic-launch thrust
share `effectiveThrust` and `integrateStep`.

### 8.2 Input mixing and scheduler

Digital input first cancels opposing left/right controls, then selects one of these exact raw
requests. Space and Up are aliases for collective; Left/H and Right/L are aliases for steer.

| Collective | Steer      | Left    | Right   | Manual `s` |
| ---------- | ---------- | ------- | ------- | ---------- |
| off        | neutral    | `0`     | `0`     | `0`        |
| on         | neutral    | `.72`   | `.72`   | `0`        |
| off        | left       | `0`     | `.375`  | `-1`       |
| off        | right      | `.375`  | `0`     | `1`        |
| on         | left       | `.4125` | `.7875` | `-1`       |
| on         | right      | `.7875` | `.4125` | `1`        |
| off        | left+right | `0`     | `0`     | `0`        |
| on         | left+right | `.72`   | `.72`   | `0`        |

The steered collective rows have total `TURNING_TOTAL=1.20`, below straight collective `1.44`, and
difference `TURN_DIFFERENTIAL=0.375`. Turn-only rows have that same difference and total `0.375`.
Thus steering never obtains the former additive forward-thrust bonus.

Primary pointer down starts at equal `.72` thrust. For horizontal displacement, retain the existing
dead zone and full-bias distance and let signed normalized `bias` be in `[-1,1]`. Resolve input
sources as intents before producing an engine pair:

```text
keyboardSteer = -1 for left only, 1 for right only, otherwise 0
pointerSteer = bias while the pointer collective is active, otherwise 0
s = keyboardSteer != 0 ? keyboardSteer : pointerSteer
collectiveActive = keyboardCollective || pointerCollectiveActive

if collectiveActive:
    base = 0.72 - 0.12*abs(s)
    halfDifference = 0.1875*s
    rawLeft = base + halfDifference
    rawRight = base - halfDifference
else:
    turnTotal = 0.375*abs(s)
    rawLeft = s > 0 ? turnTotal : 0
    rawRight = s < 0 ? turnTotal : 0
```

`pointerCollectiveActive` is true during the captured primary pointer and the retained short-tap
pulse. A nonzero keyboard steer owns the signed manual steer even while the pointer is active;
opposing keyboard steers cancel and therefore allow an active pointer bias to own it. Pointer alone
still yields exactly `(.72,.72)`, `(.75375,.56625)`, and `(.7875,.4125)` at rightward bias `0`,
`.5`, and `1`, with leftward values mirrored. At full drag it yields the corresponding
steered-collective row.

This arbitration produces one pair rather than combining engine components from independent pairs.
For collective input its total is exactly `1.44-0.24*abs(s)`, in `[1.20,1.44]`; without collective
input its total is at most `.375`. Thus no simultaneous keyboard/pointer state exceeds the straight
collective axial ceiling, and manual steer is never ambiguous. The final raw pair supplies `T` and
`s` to section 8.1. Every input, gimbal, assist, and plume value is a pure consequence of the
timestamped physical snapshot; the controller keeps no parallel physics state.

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
the target platform and its full-width solid riser, every NOC/foundation/solid `0.5 m` mast, and
non-target platforms. Interpolate position linearly and angle by the shortest arc. Set
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
and the complete riser polygon remain margin-expanded unsafe geometry. Only a bracketed true top
crossing proceeds to the safe-envelope test below, preserving ordinary pad landings without an early
top hit.

Classify the earliest contact, with unchanged equal-time precedence: building or mast, non-top
platform surface (including the solid riser), terrain, then target platform top. The target top is
safe only when both transformed feet are on its closed `9.6 m` span, neither hull side intersects an
end, `vy<=0`, and these inclusive limits hold at contact:

```text
abs(vy) <= 2.5
abs(vx) <= 1.6
abs(normalizeDegrees(angle)) <= 10.0
abs(angularVelocity) <= 15.0
```

After classifying the raw angled contact, settle upright at the exact platform center: set reference
`x` to the platform center, set reference `y` to the platform top, set angle to zero, and set both
linear velocities and angular velocity to zero. Both legal margin contacts therefore normalize to
the same deterministic settled pose before the route proof, award, or checkpoint commits. This makes
both transformed feet remain on the closed deck span and makes the later automatic launch use the
pose that the route catalog actually proves. Gas-can art and antenna signal arcs do not collide.
Contact with a consumed/powered site's platform during a later leg is unsafe; only the current
target can complete a leg.

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
| 2     | `0`         | `0.375`      | Left or H                |
| 3     | `0.375`     | `0`          | Right or L               |
| 4     | `0.4125`    | `0.7875`     | Space + Left or H        |
| 5     | `0.7875`    | `0.4125`     | Space + Right or L       |
| 6     | `0`         | `0`          | Left/H + Right/L         |
| 7     | `0.72`      | `0.72`       | Space + Left/H + Right/L |

Rows store pre-assist requests. Replay derives manual `s`, the gimbal angle, any neutral-collective
assist, effective fuel scaling, force, and torque from each current pose exactly as section 8.1.
Duplicate physical-state rows remain because the table enumerates all eight keyboard combinations;
the derivation ordering treats the lower identical command index as canonical.

### 10.2 Independent derivation tool

Keep `website/tools/derive_lander_routes.mjs` permanently. It uses only Node built-ins and must not
import production or test code; runtime, model, and tests must not import it. Version
`agw-lander-route-deriver/v2` with recipes `agw-lander-route-recipes/v2` independently implements
sections 5.2-5.3, 8, 9, and the reachable command table, including the motif-bank traversal, true
gimbal force, and neutral-collective assist. Its versioned per-template constructive recipes give
command phase order and finite integer step ranges. Each recipe evaluates at least two and at most
`2,000,000` lexicographically ordered combinations, records the exact evaluated count in its
deterministic verification output, and chooses by `(burn,totalSteps,RLE lexicographic)`, failing
rather than emitting an incomplete route. Every candidate must be safe in all nine pinned
seed/translation combinations per template. There is no beam, search heuristic, random retry,
envelope relaxation, or runtime fallback.

The exact invocation is:

```text
node website/tools/derive_lander_routes.mjs \
  --geometry website/tests/fixtures/lander-route-geometry-v1.json \
  --output PATH [--verify PATH]
```

Unknown/missing flags exit 2; derivation or verification failure exits 1; success exits 0.
`--geometry` contains schema `agw-lander-route-geometry/v1` and the nine IDs, deltas, and literal
clearance knots. Output schema `agw-lander-route-derived/v2` contains `deriverVersion`,
`recipeVersion`, exact per-route `combinationsEvaluated`, `physicsDigest`, `geometryDigest`, the
ordered route records from section 10.1, `worldWitnesses`, `worldDigest`, and `outputDigest`.
`worldWitnesses` contains exactly 81 independently reconstructed world descriptors: nine templates
times three pinned seeds times three translations. Nesting is template outermost in section 10.1
order, then seed in exact order `[1,0x12345678,0xffffffff]`, then origin translation in exact order
`[(36,3.5),(117,5),(-42,6.5)]`, where each tuple is `(originCenter,originDeckTop)`. The serialized
flat array follows that nested order without sorting or regrouping. Each descriptor includes the
selected motif-bank offset, direction, and relevant per-chunk indexes; `10 m` native/corridor
samples; cap relief; both shelf replacements and native blends; both platforms and solid risers;
exact shelf-based NOC foundations/buildings; mast colliders; and its own digest. Canonical JSON
recursively sorts object keys, preserves array order, uses `JSON.stringify` without whitespace, and
hashes UTF-8 bytes with lowercase SHA-256. `geometryDigest` hashes the complete geometry object;
`physicsDigest` hashes an object containing every named numeric constant in sections 8-10, including
gimbal and assist constants, plus the eight pre-assist command rows; `worldDigest` hashes the
ordered world descriptors; `outputDigest` hashes the output object with only `outputDigest` omitted.
The file adds one unhashed trailing LF.

The reviewed output is `website/tests/fixtures/lander-route-derived-v2.json`. Phase 4H's four-motif
bank deliberately regenerates all nine route schedules, demonstrated minima, success vectors,
one-quantum failure literals, schedule digests, all 81 ordered world descriptors and descriptor
digests, and the physics, world, and output digests in one atomic review. The production model
embeds byte-equivalent template/route arrays and the four literal digest strings. Tests project
those arrays back to the two schemas, compare canonical bytes with both fixtures, and recompute all
digests before replay. Independent test-side reconstruction compares all 81 motif selections,
corridor vertex arrays, shelf/riser/NOC descriptors, and raw/native-resume samples to production
with strict numeric equality and pins ULP-sensitive vectors so arithmetic reassociation fails. Thus
the world and tool consume identical envelope values while independently implementing corridor
construction, physics, assist, gimbal, and collision. Intentional regeneration writes to a temporary
path, uses `--verify` against the checked fixture, reviews any mismatch, then atomically updates
tool version/recipes, the derived fixture, production route/failure literals, all four digests, and
their independent expected tests. The geometry fixture changes only if its reviewed template inputs
change deliberately; never weaken clearance knots or proof bounds merely to make a recipe pass.
Ordinary tests only verify checked data and never regenerate expectations. `website/README.md` will
permanently teach this workflow. Neither fixture nor tool enters the 12-file artifact.

Catalog tests replay every literal from an upright origin with `fuel=demonstratedMinimum`, using the
exact production fixed-step gimbal/assist physics and translated shelf corridor. Each must land at
its literal success vector. A second replay with `fuel=demonstratedMinimum-FUEL_QUANTUM` must match
its literal fuel-exhaustion witness before target contact. Exhaustively validate all nine entries
and command indexes. This establishes a conservative demonstrated minimum at the catalog's fixed
schedule and fuel resolution; it makes no claim about a lower-fuel schedule or a global physical
optimum.

At site creation, direct selection translates the chosen literal geometry and constructs a
provisional checkpoint identical to the future real checkpoint except for fuel and the proof being
formed. It includes seed, the centered upright settled origin pose, completed count, next award
ratio, generator cursor, site IDs, retained descriptors, collected current can, and powered current
NOC. For each of exactly two defensive replays, replace only fuel: first with the literal
demonstrated minimum, then with one quantum less. Neither replay reads carried reserve or award
ratio. The first must reproduce success and the second its checked-in failure. Any mismatch takes
the defensive `generation-error` path; there is no runtime search, optimization, descent, retry, or
alternate-template loop. If any of the nine reviewed templates has no safe derivation inside its
finite v2 recipe and the `2,000,000`-combination ceiling, implementation stops and reports the exact
exhausted ranges and collision/fuel witnesses. It must not ship partial catalog data, widen the safe
envelope beyond section 9, lower terrain, add a third replay, or substitute runtime search.

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

In `flying`, primary pointer button 0 captures one pointer and activates pointer collective at equal
`.72` thrust. Horizontal travel produces section 8.2's pointer steer intent from:

```text
deadZone = max(10 px, scene width * 0.01)
fullBiasDistance = max(56 px, scene width * 0.18)
bias = sign(dx) * clamp((abs(dx)-deadZone)/(fullBiasDistance-deadZone),0,1)
m = abs(bias)
base = 0.72 - 0.12*m
pointerLeft = base + 0.1875*bias
pointerRight = base - 0.1875*bias
```

The last two values are the exact pointer-only result; they are not a second engine pair to merge.
Section 8.2 arbitrates the pointer intent with the keyboard intent before producing the sole raw
engine pair.

Every accepted pointer down receives a monotonically increasing token distinct from its browser
`pointerId`; reuse of a browser ID can never identify a later gesture as an earlier one. A release
within 180 ms and 10 CSS pixels retains equal thrust until
`pulseDeadline=pointerDownTimestamp+140 ms`. An eligible `pointerup` first records the completed
pointer token and deadline in the physical snapshot, schedules a token-checked timeout for any
remaining interval, records a capture-release association `{pointerId,token}`, and only then calls
`releasePointerCapture`. A lost-capture event resolves its token through that association, never by
matching a reusable `pointerId` alone.

The browser may synchronously dispatch `lostpointercapture` from that release. If its released token
equals the completed token and the deadline remains in the future, the handler clears capture-only
bookkeeping but deliberately leaves the retained pulse, completed token, deadline, timer, and queued
physical input intact. The timeout verifies the same completed token and deadline, enqueues the one
pulse-end edge, and clears them. If the 140 ms minimum has already elapsed at pointerup, completion
is immediate. `pointercancel`, a lost capture before eligible completion or for an unrelated token,
stall discard, contact, blur, hide, exit, restart, and destroy invalidate both active and completed
tokens and use the same idempotent full teardown. A resulting second event is a no-op. Flight input
is ignored during service, launch, crash, and failure. `touch-action:none` applies only to the
active flying shell; elsewhere, scrolling, zoom, text selection, and links retain browser behavior.

`destroy()` cancels the frame, listeners, media-query listener, capture, pulse timer, active ARIA,
status, and thrust; it hides and disables dead actions while leaving static recovery markup intact.
Never intercept Tab or trap focus. The header and breadcrumb remain available in every state.

## 12. Plumes, direction cue, NOC, and reduced motion

`plumeForThrust(u)` returns `scaleY=0.08+0.92*u` and `opacity=0.25+0.75*u`. The controller also
projects `commanded.vectorAngle` as `--thrust-vector-angle`: section 8.1's exact
`MAX_THRUST_VECTOR*s` while effective thrust is nonzero and zero otherwise. CSS independently scales
the external engine uses and rotates both force axes by that signed angle around their respective
`(82,401)` and `(158,401)` engine anchors. Neutral-collective assistance leaves this angle at zero
but produces visibly different plume lengths from its post-assist effective engines. Plumes affect
no collision or layout.

Scene tokens remain local and fixed: sky `#f5f2e8`, stars `#8a867c`, terrain `#d7d2c4`, outlines,
platform, and solid riser `#4b4e55`, NOC shell `#20232a`, inactive battery `#3b3f47`, battery stages
bottom-to-top `#d94a1e`, `#ff7a00`, `#ffe09a`, and `#7de2c5`, powered antenna `#d94a1e`, gas can
`#d94a1e`, and helipad marking `#f5f2e8`. Shape, outline, fill progression, and the solid `H` keep
platform, support, fuel, battery, and direction meaning independent of color.

`#lander-game` is the sole carrier of mission-wide state. Keyed `.lander-site` groups are
projections of model-owned per-site state, not independent authorities. The controller writes:

- `data-mission-state` from section 7;
- `data-paused`, `data-cue`, `data-target-offscreen`, and `data-reduced-motion`;
- `data-can="present|collected"`, `data-power="off|on"`, and `data-noc-stage="0|1|2|3|4|5"` on each
  retained keyed site group;
- custom properties `--camera-x`, `--lander-x`, `--lander-y`, `--lander-angle`, independent plume
  scale/opacity, `--thrust-vector-angle`, `--agent-x`, `--agent-y`, `--crash-x`, `--crash-y`, and
  `--crash-progress`.

The only CSS keyframes are `agw-preflight-cue` and `agw-target-cue`. Preflight runs three subtle
plume pulses over 2.4 seconds once per document load; reduced motion and START settle immediately.
The target arrow blinks at a 900 ms period only while its solid right-pointing shape is visible,
motion is allowed, and the document is active. Reduced motion leaves it continuously visible.
`data-paused="true"` pauses both keyframes.

Agent travel, battery stage, launch, and crash are model-time projections, not CSS completion
events. The battery is one vertical `22 by 40` scene-unit outline centered below the mast. Relative
to the dynamic NOC's `buildingLeft` and `roof`, its outline is
`x=buildingLeft+24,y=roof+16,width=22,height=40`; its upward terminal path is exactly
`M(buildingLeft+30,roof+16)v-6h10v6`. Four `12 by 5` horizontal bars share `x=buildingLeft+29` and
have top coordinates `roof+46`, `roof+38`, `roof+30`, and `roof+22` from stage 1 through stage 4.
This orders them bottom-to-top toward the centered mast without adding a node. The checked-in static
site uses byte-equivalent substituted coordinates.

The battery outline is always visible. At exactly 200, 400, 600, and 800 ms, stages 1 through 4
permanently fill the next bottom-to-top bar with `#d94a1e`, `#ff7a00`, `#ffe09a`, and `#7de2c5`
respectively. Stage 5 at exactly 1,000 ms changes the terminal, antenna head, and two static arcs to
powered colors; it does not recolor or reorder the bars. Every intermediate stage remains legible by
its filled-bar count and vertical direction without color. Once powered, attributes remain on that
retained site. Reduced motion creates no intermediate projection and applies all four bars plus
stage 5 atomically.

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
shake, page movement, or layout change. Section 8's active angular assist applies only to a live
thrusting lander; it never changes a fragment's stored angular velocity or ballistic formula.
Reduced motion still increments the ordinal, skips flash and fragment travel, creates exactly zero
debris nodes, and enters the same final failed state in the contact task.

## 14. Deterministic vectors

Numerical physics tests use tolerance `1e-10`; strings, integers, states, seed values, DOM order,
and serialized world descriptors are exact. Every schedule includes an explicit final callback.

| Vector                | Input                                                                      | Expected result                                                                                               |
| --------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Gravity, 120 steps    | `(10,30,0,0)`, zero angle/engines, fuel 30                                 | `x=10`, `y=28.4875`, `vx=0`, `vy=-3`, fuel `30`                                                               |
| Collective, 120 steps | Same pose, engines `(0.72,0.72)`                                           | `y=35.0215`, `vy=9.96`, angle/x unchanged, fuel `28.56`                                                       |
| Turn-only vector      | One step from same pose, raw engines `(0,0.375)`, `s=-1`                   | `ax=-1.04293235601545`, `ay=0.209815742496143`, `omega=-0.25`, angle `-0.00208333333333`, fuel `29.996875`    |
| Combined turn vector  | One step from same pose, raw engines `(0.4125,0.7875)`, `s=-1`             | `ax=-3.33738353924943`, `ay=7.27141037598766`, `omega=-0.25`, angle `-0.00208333333333`, fuel `29.99`         |
| Angular assist        | One step, angle `0`, omega `15`, raw engines `(0.72,0.72)`                 | engines `(0.66,0.78)`, `s=0`, omega `14.92`, angle `0.124333333333`, fuel `29.988`; total thrust unchanged    |
| Vacuum coast          | One step, angle `0`, omega `15`, zero engines                              | omega remains `15`, angle `0.125`, `vy=-0.025`; no translational or angular damping                           |
| Exhaustion            | Fuel `0.005`, one step, engines `(1,1)`                                    | Effective engines `(0.3,0.3)`, fuel exactly `0`                                                               |
| Pointer vectors       | Rightward normalized drag `m=0,0.5,1`                                      | `(.72,.72)`, `(.75375,.56625)`, `(.7875,.4125)`; leftward values mirror exactly                               |
| Mixed input ceiling   | Keyboard collective plus pointer full right                                | pointer owns `s=1`; engines `(.7875,.4125)`, total `1.2`, never component-combined                            |
| Keyboard steer owner  | Keyboard left plus pointer full right                                      | keyboard owns `s=-1`; engines `(.4125,.7875)`, total `1.2`                                                    |
| Canceled steer owner  | Both keyboard steers plus pointer half right                               | keyboard cancels; pointer owns `s=.5`; engines `(.75375,.56625)`, total `1.32`                                |
| Empty-fuel direction  | Fuel `0`, raw engines `(.7875,.4125)`, retained physics `s=1`              | effective engines `(0,0)` and stored/rendered `commanded.vectorAngle=0`                                       |
| Plumes                | `u=0,0.5,1`                                                                | scales `0.08,0.54,1`; opacities `0.25,0.625,1`                                                                |
| First site            | Any normalized seed                                                        | ID `0`, center `36`, width `9.6`, shelf `[31.2,49.8]`, top=`shelf-span native maximum+0.8`, NOC bottom=shelf  |
| Pad parity            | Static and dynamic site with platform top `p`                              | shelf `p-0.8`; deck underside `p-0.35`; one solid riser polygon spans full width and exact `.45 m` interval   |
| Ratio                 | Start at `3`; apply `nextAwardRatio` successively                          | `3`, `2.64`, `2.3448`, then strict decrease to constant `1+Number.EPSILON`; O(1) per call                     |
| Safe inclusive edge   | Target top; `vx=1.6,vy=-2.5,angle=-10,omega=15`                            | safe contact                                                                                                  |
| Unsafe epsilon        | Any one safe magnitude increased by `1e-9`                                 | unsafe contact                                                                                                |
| Swept unsafe equality | Hull only grazes a terrain/riser/mast edge between step endpoints          | closed 0.02 m expansion detects it; no visual tunneling                                                       |
| Target-top separation | Safe descent over deck center; then a separate exact tangential graze      | descent uses true top crossing and can be safe; unresolved graze is unsafe                                    |
| Frame equivalence     | Initial approach, no input, callbacks to 1,000 ms at 30, 60, and 120Hz     | 120 steps; `x=30.8`, `y=30.0875`, `vx=0.8`, `vy=-3.4`, fuel `30`                                              |
| Checkpoint replay     | Award, launch, crash, RESTART twice                                        | identical post-award fuel/site flags/next ratio; no can, award, ratio, or progress duplication                |
| Catalog quantum       | Every checked-in reference template                                        | allowance `minimum` matches literal safe contact; `minimum-0.05` matches literal failure                      |
| Short-tap capture     | Down at `0`, eligible up at `20`; release synchronously emits lost capture | token/deadline exist before release; pulse remains through `139.999`, ends once at `140`; later loss is no-op |
| Input overflow        | 65 alternating edges before one step at 30, 60, and 120 Hz                 | queue becomes one next-step physical-state snapshot; all frame schedules produce the same result              |
| Long run              | 100 successful deterministic sites                                         | fixed work per ratio advance; bounded nodes/edges; reserve equals initial plus awards minus all burn          |

World tests pin complete JSON descriptors and route-proof digests for seeds `1`, `0x12345678`, and
`0xffffffff`, plus an independently authored static-scene vector. The fixtures begin with these
exact values; traversal is `offset,direction; motifIndex(q=0..3)`:

| Seed                | `mixUint32(seed)` | Traversal      | Chunk 0 heights                                                                                                  | Site 0 top           | Leg-1 template preference     |
| ------------------- | ----------------- | -------------- | ---------------------------------------------------------------------------------------------------------------- | -------------------- | ----------------------------- |
| `1`                 | `1753845952`      | `0,1; 0,1,2,3` | `3.948548639542423,6.055567677598447,1.8625867156544702,4.869605753710493,1.6766247917665167,2.4836438298225403` | `5.286448038277216`  | `102,87,99,84,96,81,93,78,90` |
| `0x12345678`        | `4125564054`      | `2,3; 2,1,0,3` | `2.8413594241719693,3.9342738820938394,5.72718834001571,4.02010279793758,1.7130172558594494,3.8059317137813196`  | `4.564073424622881`  | `99,84,96,81,93,78,90,102,87` |
| `0xffffffff`        | `1734902346`      | `1,1; 1,2,3,0` | `2.9631244149059057,0.763576190569438,1.9640279662329705,4.864479741896503,3.564931517560035,2.4653832932235673` | `5.5085339549761265` | `87,99,84,96,81,93,78,90,102` |
| `STATIC_WORLD_SEED` | `1076842847`      | `3,1; 3,0,1,2` | `4.29865836398676,3.1665419081225994,6.134425452258438,7.5,4.870192540530115,5.638076084665954`                  | `7.984423104863613`  | `78,90,102,87,99,84,96,81,93` |

The static row is the exact no-JavaScript site-0 descriptor for `0x41475731`; it is not an extra
seed in the 81 derived descriptors. The implementation commit also records literal template
schedules, success/failure vectors, envelopes, instantiated-site descriptors, and proof digests from
section 10's independent derivation; tests must not generate expected values by calling the function
under test. For each pinned derived seed, tests cover at least three sites, the exact motif bank and
traversal across positive and negative chunks, terrain diversity, preference and eligibility order,
guaranteed zero-delta selection, contact-time offscreen placement, both proof replays, exact award,
and rolling-window eviction.

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

| Layer                                                                   | Required coverage                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `node --test website/tests/lander-world.test.mjs`                       | Mixer/seeds; exact `10 m` samples and `50 m` shared chunk boundaries; all four motif literals, pinned offset/direction vectors, positive and negative chunk traversal, and four distinct indexes in every four-chunk witness; boundary/motif/clamp diversity; every shelf/corridor pseudocode branch, global index, cap equality, relief, complete target replacement, both native blends and deduplication; site-0 band and zero-delta guarantees; exact geometry digest; static/dynamic shelf, riser, NOC foundation, can, window, retention, offscreen, and immutability                                                  |
| `node --test website/tests/lander-model.test.mjs`                       | State/events; executable synchronous release/lost-capture short-tap token witness and unrelated-loss teardown; 9.0/80 physics, all digital/pointer rows, mixed-source steer ownership and axial ceiling, true gimbal force/sign, zero-effective vector reset, neutral-collective assist, total/fuel preservation, manual override and undamped coast; carry/scheduler/overflow; closed-margin riser/terrain/NOC equality/tangency, unchanged precedence, exact top safe crossing and grazing failure, inclusive limits and `+1e-9`; v2 catalog/digests, two replays, ratio/checkpoint/generation error/launch/debris/ordinal |
| Derivation CLI fixture verification                                     | Run section 10.2's command to a temporary output with `--verify website/tests/fixtures/lander-route-derived-v2.json`; exact v2 deriver/recipe/schema; per-template counts in `[2,2,000,000]`; all nine regenerated minima/success/failure literals; all 81 strict world descriptors/digests in exact template, seed `[1,0x12345678,0xffffffff]`, translation `[(36,3.5),(117,5),(-42,6.5)]` nesting; deterministic bytes; finite-exhaustion failure and nonzero mismatch/usage exits; import closure proves independence                                                                                                     |
| `python -m unittest discover -s website/tests -p 'test_*.py'`           | Exact 12-file artifacts at both bases, excluding tools/fixtures; focused validation helper; exact game-to-model/world, model-to-world, world-to-none module DAG; byte-equivalent static/dynamic shelf/riser and vertical battery subtree; transactional-init structure and hidden/disabled static Start; fuel/actions; local SVG/CSS; forbidden network, storage, audio, canvas, service-worker, navigation, cookie, and uncontrolled randomness                                                                                                                                                                             |
| Manual Chrome and Edge pre-merge; Firefox and Safari/WebKit post-launch | Start/focus; injected initialization failures restore exact static DOM; Space/arrows/vi/touch, a short tap surviving automatic lost capture through its deadline, simultaneous keyboard/pointer ownership and axial ceiling, pointer vector direction, gimbaled plumes, idle/exhausted vector reset, and visible assist; three sites across four different coarse terrain motifs; no pale pad aperture; flat pad/NOC shelves; bottom-to-top colored power stages; can/arrow/carry/empty fuel; relaxed boundary landings and over-bound crashes; checkpoint/Exit/hidden pause; zero game requests                             |
| Manual responsive and accessibility acceptance                          | 320 CSS pixels, 400 percent zoom, touch landscape, and wide viewport; no overflow or clipped actions; 44-pixel targets; logical focus; no trap; useful no-CSS/no-JS order; named fuel value; restrained live announcements; solid direction cue; static reduced-motion cue; silent decorative SVG; fixed-token 4.5:1 text and 3:1 necessary-graphic contrast                                                                                                                                                                                                                                                                 |
| Performance and longevity witness                                       | 100-site deterministic run; no more than five terrain paths, three sites, eight fragments, or 80 world descendants; direct selection/shelf-corridor/exactly-two-replay timing recorded; hidden tab has no frame or mission progress; normal active frame p95 below 4 ms; teardown leaves no listener, timer, capture, frame, enabled dead action, or growing retained history                                                                                                                                                                                                                                                |
| Permanent documentation and repository gates                            | `website/README.md` teaches the tuned controls/physics and v2 intentional-regeneration workflow; browser checklist pins the visual/input/vacuum witnesses; file lint, locked-SDD, Rulesync drift, diff check, and module-size report pass without linking permanent docs back to this SDD                                                                                                                                                                                                                                                                                                                                    |

Mutation tests reject duplicated/moved shared markup, a second scheduler/controller/site authority,
game checks added to the near-limit validator, artifact count drift, a sixth retained chunk,
pad-width/elevation drift, a visual-only support fill or non-solid riser collider, an extra support
node, a production import outside the exact DAG, `10 m`/`50 m`/boundary/motif-bank/selector/clamp
drift, a single or repeated terrain motif, a shelf ending before or after `platformRight+9`,
native-derived NOC foundation, hidden shelf easing, fuel caps, can recollection, proof dependence on
carried fuel, component-wise keyboard/pointer engine merging, mixed-input thrust above `1.44`,
pointer override of a nonzero keyboard steer, canceled keyboard steer blocking an active pointer, an
idle or exhausted nonzero `commanded.vectorAngle`, recording a completed pointer token after capture
release, clearing its live pulse on the resulting lost-capture event, ignoring unrelated lost
capture, matching pulse completion by reusable pointer ID instead of token, an unguarded or
duplicate pulse timeout, route proofs that omit launch, ratio recomputation from `completedSites`,
any runtime planner/search/fuel scan or third proof replay, a catalog command outside the reachable
table, passive damping, assist while coasting or steering, assist that changes total thrust/fuel,
reversed or cosmetic-only gimbal, stale 8.4/70 integration, old landing limits,
horizontal/reversed/mistimed battery fill, color-only battery meaning, production-derived expected
fixtures, v1 derived output, partial route/world regeneration, wrong world-witness seed/translation
nesting, derivation-tool imports, motif selection, corridor, or 81-descriptor digest drift, open or
unmarginated unsafe collision, margin-expanded target top, partial initialization residue,
retained-node growth, monotonic-furthest-X camera state, zero normal-motion debris, assist applied
to fragments, animated-only direction, atmospheric crash effects, or a durable/network surface.

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
| Phase 4H: terrain, support, control, landing, and NOC tuning           | Sections 4-6, 8-10, 12, 14, and 15   |

Implementation treats this LLD as temporary design input. Permanent source, tests, and
`website/README.md` stand on their own and do not link back to this SDD path.
