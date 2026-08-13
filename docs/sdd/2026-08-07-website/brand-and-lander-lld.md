# LLD: AGW Brand and Continuous Lunar Deployment Lander

<!-- cspell:ignore arcade Cascadia Consolas focusout IDREF IDREFs imul keyup Menlo -->
<!-- cspell:ignore pointerdown pointerup PRNG -->
<!-- cspell:ignore letterboxing parallax refuel reproject reprojection reprojects repower Segoe -->
<!-- cspell:ignore lerp Minkowski overspeed subinterval unhashed unmarginated -->
<!-- cspell:ignore substep underframe unitless uint32 quantized quantization Warren -->

- Status: Phase 4P rejected and superseded; Phase 4O behavior restored
- Operator browser acceptance: pending
- Date: 2026-08-12
- FRD: `frd.md`, specifically R6-R9, R15-R24, and R25
- HLA: `hla.md`, specifically D5 and D7
- Selected geometry: `logo-concept-10-twin-flame.svg`

## Supersession record

Phase 4P's broad-relief implementation was completed and reviewed, then rejected by the operator on
2026-08-12. Its normalized relief kernel, fixed global 11.6 m platform datum, variable tall support
columns, vertical camera projection, collision optimization, responsive-gauge-only workaround, v5
geometry and derived fixtures, and Phase 4P-specific tests are superseded. Commit
`56650d774e4ba3769ca293072632e5741d493396` is restored as the current Lander behavior while a new,
separately designed terrain-relief change is considered. The completed Phase 4P record in the
lead-owned plan remains historical evidence, not a claim about current production behavior.

## 1. Scope and terms

This LLD preserves the selected brand and defines the continuous Lander and arcade presentation in
R7-R9 and R21-R25, including the Phase 4M free-exploration, lattice-column, parallax-sky,
half-reference opening, and faster-deployment refinement. It excludes main-page, onboarding,
deployment, and DNS design. Use plain HTML, CSS, SVG, and JavaScript.

A **run** begins at START and ends at Exit or reload. A run contains successive **legs**, each from
one checkpoint or the initial approach to one target site. A **site** is one platform, gas can, and
NOC. **Commanded thrust** is the post-input, post-assist, post-fuel engine value shared by physics
and plumes. **Manual steer** is the normalized signed turn intent before angular assistance;
negative is left and positive is right. **Thrust-vector angle** is the manual-steer-derived
direction shared by both engine forces and both rendered plumes while effective thrust is nonzero;
it is zero at zero effective thrust. **Mission time** excludes hidden time. A **demonstrated
minimum** is the smallest fuel allowance, at the pinned fuel quantum, that completes one checked-in
constructive reference schedule. It is not a global mathematical optimum over all possible controls.
The **refuel ratio** for the one-indexed successfully powered base number `n` is exactly
`ratio(n)=1+0.5^(n-1)`. The first successfully landed and powered base is `n=1`. That mathematical
sequence approaches `1` from above; section 10.3 pins its exact JavaScript Number projection,
including the finite-precision value `1` from `n=54` onward. The **fuel-gauge reference** is the
positive model-owned denominator used only by the visual gauge. It starts at `30` against the
opening reserve of `15`, then becomes the exact uncapped post-award reserve whenever an award
establishes a later leg. It is neither a tank capacity nor necessarily the amount present when an
initial leg starts. A **refuel projection** is the model-owned, 300 ms presentation record that
starts at the pre-award gauge level while the already-committed fuel award remains authoritative. An
**installed agent** is the visual projection of a retained site's existing `nocStage`/`powered`
state into its existing NOC-entry path; it is not a second site-state field or world node.
**Launch-ready** means the centered powered-pad checkpoint is holding the lander at rest before the
player's first effective collective command. It is represented by `state="launching"` with
`launchStarted=false`, not by another mission state or a controller-owned flag.

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
| `website/tools/lander_clear_faces.mjs`                 | Independent scaffold-overlay and clear-face enumeration         |
| `website/tests/fixtures/lander-route-geometry-v4.json` | Canonical site, template, and envelope geometry input           |
| `website/tests/fixtures/lander-route-derived-v4.json`  | Reviewed independent schedules and witness output               |
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

`lander-game.js` imports the model API plus only the pure `cameraLeftForPose`, `CHUNK_WIDTH`,
`mixUint32`, `siteScaffoldPath`, `siteStructure`, `skyProjectionForCamera`,
`skyProjectionIdentityForCamera`, `targetDirectionForViewport`, `terrainFillPath`,
`terrainSurfacePath`, and `terrainVerticesForRange` exports directly from `lander-world.js`.
`lander-model.js` imports pure world construction, retention, seed, and geometry exports.
`lander-world.js` imports neither production module, reads no DOM, clock, storage, or ambient
randomness, and owns no mutable singleton. No production module imports upward or sideways outside
this DAG.

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
    div#lander-scene-stage
      svg#lander-scene[viewBox="0 0 1000 640"]
        title#lander-scene-title
        desc#lander-scene-description
        rect#scene-sky
        g#lander-sky-world[aria-hidden="true"]
          path#scene-stars
          path#scene-landmarks
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
        span#lander-fuel-gauge[aria-hidden="true"]
          span#lander-fuel-gauge-fill
        span#lander-fuel-label.visually-hidden "Fuel reserve:"
        span#lander-fuel-value.visually-hidden
      span#lander-target-direction.visually-hidden[hidden]
      div#lander-outcome[hidden]
        p#lander-status[role="status"][aria-live="polite"][aria-atomic="true"]
        button#lander-restart[type="button"][hidden][disabled][aria-keyshortcuts="r"]
          span.lander-action-label "Retry"
          span.lander-key-hint[aria-hidden="true"] "r"
    div#lander-controls-rail[hidden]
      p#lander-controls
        span.lander-controls-line.lander-controls-keyboard "Space/Up thrust; Left/H or Right/L turn."
        span.lander-controls-line.lander-controls-touch "Touch: tap/hold to thrust; drag to turn."
      button#lander-exit[type="button"][disabled][aria-keyshortcuts="Escape"]
        span.lander-action-label "Exit mission"
        span.lander-key-hint[aria-hidden="true"] "<esc>"
```

The template contains the complete static first terrain window, site 0, lander, gas can, and dark
NOC inside the listed layers. Its `.terrain-fill` and `.terrain-surface` paths cover the complete
`0..1000` scene width before JavaScript and use section 6's same contiguous range-clipping
projection as runtime; no open right-half sky, internal chunk closure, or visual/collision gap is
permitted. Enhancement reconciles those same two nodes rather than keeping a hidden second world or
creating per-chunk paths. Each retained site is one
`.lander-site[data-site-id][data-can="present|collected"][data-power="off|on"]`
`[data-agent="absent|installed"]` containing, in order, `.landing-platform`, `.site-scaffold`,
`.gas-can`, `.noc-building`, `.noc-battery`, and `.noc-antenna`. The single unfilled
`.site-scaffold` path contains section 5.3's exposed narrow members; it adds no wrapper, backing
rectangle, or decorative child. The battery contains one rectangular outline and four bars, with no
terminal path. Three sibling `.antenna-signal` paths own the final signal stages. Reconciliation
keys by the integer data value and replaces no stable outer layer. Decorative world descendants
remain `aria-hidden`.

The controls node has exactly two direct prose children in this order, each on its own visual line:

```text
Space/Up thrust; Left/H or Right/L turn.
Touch: tap/hold to thrust; drag to turn.
```

That wording is implementation and human-review authority, not a unit-test string fixture. Automated
witnesses pin that `#lander-controls` is the rail's non-interactive prose child, that it contains
exactly the keyboard span followed by the touch span and no control, and that it is the node
referenced by `aria-describedby`; they do not assert either authored line or a phrase blacklist. The
rail container itself is never an accessible description because it also contains Exit. The two
`.lander-key-hint` spans are visible presentation only. Their `aria-hidden="true"` keeps the native
buttons' accessible names equal to their visible `.lander-action-label` text, exactly `Retry` for
the crash action and `Exit mission` for Exit, while `aria-keyshortcuts="r"` and
`aria-keyshortcuts="Escape"` expose the same shortcuts semantically. The rendered hint strings are
exact lowercase `r` and exact `<esc>`, with `<esc>` authored as `&lt;esc&gt;` in HTML. No
`aria-label`, `title`, generated content, or duplicated offscreen string overrides either native
name. Automated accessible-name witnesses compare each computed name to its normalized visible label
source and independently prove the hidden hint is excluded; exact authored wording remains a
human-review assertion.

Preflight hides fuel, outcome, controls rail, direction cue, crash presentation, and all
instructions. Initialization is transactional. Before mutation, clone `#lander-game` as the pristine
recovery snapshot; build the model and controller in locals; and register every listener, media
query, animation frame, timer, and pointer-capture cleanup in one LIFO teardown registry. Render
preflight, then reveal and enable Start as the final operation before publishing the controller
reference. If any operation throws, run that same registry in reverse while catching each cleanup so
later entries still run, replace any partially changed game root with the snapshot, leave the
controller reference null, and report only to the console. The restored Start is hidden and
disabled, static SVG naming/ARIA is exact, and no listener, model, timer, capture, or mission
attribute survives. `destroy()` consumes the same idempotent registry. Absent JavaScript also
preserves this static recovery. START reveals fuel, outcome, and the controls rail, enables Exit,
and leaves Retry hidden and disabled. The outcome stays in the accessibility tree throughout an
active run so its live status can announce transient service messages; `data-banner="none"` clips
the panel visually rather than hiding it. `data-banner="deployed"`, `"crashed"`, and `"error"`
project only launch-ready, failed, and generation-error respectively. Only `failed` reveals and
enables Retry beneath the crash status. Exit remains visible and enabled at the bottom-right of the
controls rail for every active state. The resulting game-subtree tab order is shell, Exit in every
active non-failed state and shell, Retry, Exit while failed. The adjacent ordinary
`span#lander-fuel-label` and `span#lander-fuel-value` are the only accessible fuel-description
sources. Both use the established `.visually-hidden` class and neither has `aria-labelledby`,
`aria-describedby`, `aria-label`, `role`, `aria-live`, or implicit live semantics. The shell's
ordered `aria-describedby` IDREFs name the label and then the value separately, so the computed
accessible description contributes both the label text and changing numeric text instead of
substituting the label for the value. The adjacent gauge and fill are `aria-hidden` and cannot
become a second meter, progress element, live region, or named control. The controller sets only the
value span's `textContent` to `fuel.toFixed(1)` when that displayed tenth changes. Model fuel
remains the exact unrounded number; this one-decimal accessible presentation is intentionally
rounded and is not an exact decimal encoding. Fuel is never announced through status.

The controller owns this exact projection; `shown` means the element and every ancestor are not
hidden, and `enabled` applies only to native buttons:

| State projection                                      | Start            | Outcome / banner | Retry            | Controls rail / Exit  |
| ----------------------------------------------------- | ---------------- | ---------------- | ---------------- | --------------------- |
| Source no-JS or failed initialization                 | hidden, disabled | hidden           | hidden, disabled | hidden; Exit disabled |
| Successfully initialized `preflight`                  | shown, enabled   | hidden           | hidden, disabled | hidden; Exit disabled |
| `flying`, service, started `launching`, or `crashing` | hidden, disabled | shown / `none`   | hidden, disabled | shown; Exit enabled   |
| launch-ready `launching`                              | hidden, disabled | shown / deployed | hidden, disabled | shown; Exit enabled   |
| `failed`                                              | hidden, disabled | shown / crashed  | shown, enabled   | shown; Exit enabled   |
| `generation-error`                                    | hidden, disabled | shown / error    | hidden, disabled | shown; Exit enabled   |

On start, the controller hides and disables `#lander-start`, gives `#lander-scene-shell`
`tabindex="0"`, `role="application"`, `aria-label="Lunar deployment game"`, and an
`aria-describedby` list ordered exactly as
`lander-scene-description lander-controls lander-fuel-label lander-fuel-value`, optional
`lander-target-direction`, then `lander-status`. It references the controls prose node, never
`lander-controls-rail`. Target direction is present only while the exact offscreen predicate is
true. The controller renders that relationship before focusing the shell with `preventScroll: true`.
Exit removes active attributes, restores `tabindex="-1"`, disables Exit, hides the controls rail and
other active chrome, reveals and enables Start, and focuses Start without scrolling. Retry clears
accepted held keys, queued edges, pointer capture, and the tap pulse; dispatches the model's
internal `RESTART` event; resets the controller frame timestamp and accumulator without stepping;
renders the restored model; hides and disables itself; then focuses the active shell with
`preventScroll:true`. The lowercase `r` shortcut invokes that same ordered handler only from the
active shell path. Neither a crash nor launch readiness moves focus, so focus remains wherever it
was until Retry succeeds. While active, the shell and Exit are always tabbable; Retry is the one
additional game-action tab stop only while failed. Every native action has `min-inline-size:44px`
and `min-block-size:44px`; its visible text remains its accessible name without an overriding ARIA
label. Hidden Retry remains disabled and outside sequential focus order. Initialization recovery,
destroy, Exit, contact, failure, and Retry restore these exact hidden/disabled/focus invariants.

The DOM ID `lander-restart`, controller property names that refer to it, and the model event
`RESTART` may remain internal implementation identifiers. They are not visible copy or a second
action authority. All user-visible, accessible-name, documentation, and manual-test surfaces call
the action Retry.

Before activation the SVG is a named image whose description mentions the hovering lander, varied
lunar surface, elevated platform, gas can, and dark NOC, but no controls. While the shell is an
application, the SVG is `aria-hidden`; the live status conveys outcomes and the two non-live fuel
description sources expose reserve on demand. With JavaScript unavailable, Start and all active
chrome remain hidden while the named static scene, page heading, 404 explanation, and breadcrumb
remain useful.

The dedicated page's document title is exactly `We need agents! | Agentworks`, and its visible `h1`
is exactly `We need agents!`. The 404 retains `Page not found`; its following explanatory paragraph
is exactly `This route is broken! We need agents!`. The shared scene and active-game accessible
names remain `Lunar deployment scene` and `Lunar deployment game`, because they name the activity
rather than repeat either page title. These shell copy changes do not fork the shared fragment,
breadcrumb recovery, or no-JavaScript behavior. These title, heading, explanation, controls, status,
and action-label strings remain implementation and human-review authority. Implementation deletes
existing automated equality, substring, presence/absence, and blacklist assertions over
repository-authored prose. Tests retain structural, state, role, IDREF, accessible-name-source, and
live-region assertions; when a real accessibility tree witness needs label text, it reads and
normalizes the current DOM text instead of embedding an expected phrase.

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
acquire a fresh seed. Retry reuses the current run seed and checkpoint.

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
slopes, and no three equal consecutive native samples. A single motif or repeated motif selection
fails even if its sampled heights happen to retain rises and falls.

`terrainHeightAt(seed,x)` linearly interpolates the enclosing sampled edge. Terrain is collision
geometry, not merely art. Chunk indexes remain generation and retention bookkeeping only; they are
not independently closed render polygons. Section 6 projects one strictly increasing retained-window
surface chain so no internal chunk edge can become a vertical terrain stroke.

### 5.3 Sites and constructive template selection

Site 0 has fixed platform center `x=36 m`. Native terrain remains byte-for-byte present beneath the
complete platform, gap, truss, and NOC footprint; there is no site shelf, flat replacement, blend,
or NOC foundation terrain. Define `platformLeft=platformCenter-4.8`,
`platformRight=platformCenter+4.8`, `buildingLeft=platformRight+2`, and
`buildingRight=buildingLeft+7`. For a site candidate, compute

```text
nativeMaximum = max y in nativeTerrainVertices(seed,platformLeft,buildingRight)
minimumDeckTop = nativeMaximum + 2.4
DECK_LEVELS = [83,91,99] integer decimeters
```

The maximum includes the two closed range endpoints and every intervening `10 m` native sample;
piecewise linearity proves no omitted point is higher. Site 0 takes the first level in `DECK_LEVELS`
whose `level/10 >= minimumDeckTop`. Because native terrain is clamped to at most `7.5 m`, level `99`
always qualifies. Storing `deckLevel` as the integer `83`, `91`, or `99` and deriving
`platformTop=deckLevel/10` prevents accumulated binary rounding from creating a fourth height. The
checked-in no-JavaScript scene is the exact site-0 descriptor for `STATIC_WORLD_SEED`; START
reconciles it to the fresh run seed before the first flight frame. Every later target is a
translation of one of section 10's nine constructive templates. The catalog maps center delta to
deck-height delta exactly:

```text
center delta:  78   81    84    87    90    93    96    99   102 m
deck delta:     0  +1.6  -0.8  +0.8  -1.6    0   -0.8  +0.8    0 m
```

For site index `i`, let `base=floor(9*sampleUnit(seed,3,i))` and inspect all nine catalog slots in
the order `slot=(base+4*c)%9` for `c=0..8`. Four and nine are relatively prime, so this is one
complete seed-rotated permutation with no duplicate or omitted template. Convert the template delta
to exact integer decimeters with `deltaLevel=Math.round(deckDelta*10)`, and select the first
candidate whose `targetLevel=origin.deckLevel+deltaLevel` is a member of `DECK_LEVELS` and whose
`targetLevel/10 >= minimumDeckTop(seed,targetCenter)`. This is at most nine constant-time
eligibility checks, not route search. Termination is structural: level `83` has the `+16` route to
`99`, level `91` has either `+8` route to `99`, and level `99` has the three zero-delta routes; a
`99` target always clears the `7.5+2.4=9.9 m` native upper envelope. Every permutation therefore
reaches an eligible entry. The preference retains seeded distance and elevation variety without an
unbounded terrain or route search. No entry is a dead fallback or a special Retry path.

Each template carries collision-safe `clearanceKnots` relative to the origin center/deck. Relative
knot heights are at least `-0.65 m`, so every eligible translation has absolute cap at least
`2.5 m`. `lander-world.js` and the independent tool implement this exact construction separately:

```text
native(n):
  x = 10*n; q = floor(n/5); k = n-5*q
  if k == 0: raw = boundary(q)
  else: raw = lerp(boundary(q),boundary(q+1),k/5) + MOTIFS[motifIndex(q)][k]
  return clamp(raw,0.5,7.5)

originBuildingRight = originCenter+13.8
targetPlatformLeft = targetCenter-4.8
X = every native 10 m sample in the generated range
    union every retained site's platformLeft, platformRight, buildingLeft, buildingRight
    union every retained site's six lattice-column rail X coordinates
for each x in X in strict numeric order:
  raw = terrainHeightAt(seed,x)
  if x is an integer 10 m sample and originBuildingRight < x < targetPlatformLeft:
    cap = originTop + piecewiseLinear(clearanceKnots,x-originCenter)
    y = raw > cap ? max(0.5,cap-0.15*sampleUnit(seed,4,(x/10)>>>0)) : raw
  else:
    y = raw
  emit exactly one (x,y)
join every adjacent emitted vertex with one straight segment
```

`boundary`, `MOTIFS`, and `motifIndex` are exactly section 5.2; equality at the cap gets no relief.
`raw` is `terrainHeightAt(seed,x)` when `x` is not a global sample. The set union occurs before
projection; it cannot emit two values for one X, and the final array must satisfy
`vertices[j-1].x < vertices[j].x` for every `j`. Exact chunk, site, and column-rail insertions
therefore lie on the same native segment and share the same numeric Y authority on both sides.
Relief is permitted only at native `10 m` samples strictly in the open flight corridor; native
terrain is untouched over both closed site footprints and at both corridor endpoints. There is no
discard/resume splice, same-X conflict resolution, vertical collision edge, shelf easing, or hidden
foundation. When the retained window contains three sites, apply this rule independently to the two
open corridors from their immutable adjacent-leg descriptors; the intervals are disjoint, and every
other X uses native terrain. Forward or backward reconciliation therefore reproduces the same Y for
every retained X without mutable terrain history. The model passes frozen geometry to pure
`instantiateTemplateSite(seed,siteIndex,originSite,templateGeometry)`; the world never imports or
selects the model catalog. Exact serialized vertices for every template at multiple translations and
the ordered seeds/translations in section 10.2 catch predicate, motif index, clamp, relief, strict-X
ordering, every inserted boundary, and native continuity beneath both structures.

A catalog/schema or replay mismatch is an invariant error: site state is left unchanged, the run
enters `generation-error`, and the live status becomes exactly
`Mission generation failed. Use Exit mission to start a new run.` No unreachable target appears,
Retry remains hidden and disabled, and `r` is ignored. Exit remains available and the next START
gets a fresh seed. This path defends against implementation corruption only; ordinary generation
cannot exhaust its template choices or enter an unbounded retry loop.

Each platform is exactly `3 * 3.2 = 9.6 m` long, centered at the site coordinate, and `0.35 m`
thick. Define `platformBottom=platformTop-0.35`. One visually continuous shallow open Warren-style
truss spans exactly `buildingRight-platformLeft=18.6 m`, from beneath the platform through the
former connector region and beneath the complete NOC. It has no platform, connector, or NOC region
partition and no region-specific scaffold X fields.

One `.site-scaffold` path renders every exposed member, with no filled face behind it. In scene
coordinates, let `L=platformLeft*10`, `Q=buildingRight*10=L+186`, `T=548-platformBottom*10`,
`B=T+7.5`, and `X_i=L+15.5*i` for integer `i in [0,12]`. The path begins with the two uniform chords
`M L T H Q M L B H Q`. Append exactly one diagonal per bay in increasing bay order: for even
`i in [0,11]`, `M X_i T L X_(i+1) B`; for odd `i`, `M X_i B L X_(i+1) T`. The first diagonal
therefore descends from the top-left, and successive diagonals alternate across all 12 equal
`1.55 m` bays without resetting at the platform or NOC boundaries.

Exactly three visible load-bearing open lattice columns join that truss. Their rail pairs, relative
to `platformLeft`, are exactly `[0,1]`, `[8.8,9.8]`, and `[17.6,18.6] m`. The first and last outer
rails therefore align with the complete structure's ends, while the middle pair is centered on the
span midpoint at `9.3 m`. All six rails remain inside the closed site footprint used by the `2.4 m`
native-clearance proof. For a column with world rail positions `(A,D)`, independently set
`footA=terrainHeightAt(seed,A)`, `footD=terrainHeightAt(seed,D)`, `columnTop=platformBottom`, and
`latticeFloor=max(footA,footD)`. Set `COLUMN_WIDTH=1 m` and `COLUMN_BAY_HEIGHT=.8 m`.

Let `bayCount=ceil((columnTop-latticeFloor)/.8)`. Its ordered horizontal levels are
`Y_i=max(latticeFloor,columnTop-.8*i)` for `i=0..bayCount`; duplicate terminal levels are forbidden.
Append, for each column from left to right, two complete rails from `(A,columnTop)` to `(A,footA)`
and `(D,columnTop)` to `(D,footD)`, one horizontal tie from `A` to `D` at every `Y_i`, and one
diagonal in every bay. Even bay `i` descends from `(A,Y_i)` to `(D,Y_(i+1))`; odd bay `i` descends
from `(D,Y_i)` to `(A,Y_(i+1))`. The first brace therefore continues the shallow Warren truss's
top-left-to-bottom-right rhythm. The top tie overlaps the truss's top-chord centerline, so each
column reads as joined rather than hung beneath it. Both rails reach their independently sampled
native feet; below the bottom tie only the rail on the lower side continues through the small
terrain-slope wedge. The `2.4 m` deck clearance and `.35 m` platform thickness prove
`columnTop-latticeFloor>=2.05 m`, so every column has at least three bays. There are exactly
`3+2*bayCount` column members, between 9 and 27 under the `[.5,7.5] m` terrain and three deck-level
bounds. The complete single path therefore contains the fixed 14 truss members plus three bounded
column member sets, between 41 and 95 straight segments. It still creates no child node, backing
face, regional brace field, crossing X pair, or common foundation.

Set `fill="none"`, `stroke="#4b4e55"`, `stroke-width="2"`, `stroke-linecap="butt"`, and
`stroke-linejoin="round"`; CSS cannot supply a fill, background, or sky-colored rectangle. The
members are exactly `0.2 m` thick and contrast `7.440:1` against sky and `5.517:1` against terrain.
Butt-capped member endpoints meet on the chord center lines, so their half-width stroke areas
overlap without extending beyond an endpoint. The round join remains explicit for each continuous
chord.

Let `MEMBER_HALF=0.1 m` and `trussBottom=platformBottom-0.75`. The exact truss collider is
`[platformLeft-.1,buildingRight+.1] x [trussBottom-.1,platformBottom+.1]`, or relative to platform
center `[-4.9,13.9] x [-1.2,-0.25]`. Name it `truss`; delete the former `platformUnderframe`,
`connector`, and `nocUnderframe` descriptor and collider fields. This single closed rectangle is the
exact axis-aligned `+/-0.1 m` expansion of the two chord extents and conservatively contains every
diagonal. Its top is `platformTop-0.25`, safely below the unexpanded landing face, so a legal
target-top contact cannot hit it first. Each exact column collider is the closed stroked-member
axis-aligned bounding box `[A-.1,D+.1] x [min(footA,footD)-.1,columnTop+.1]`. It contains both
rails, every tie, every brace, and both butt-capped native feet, overlaps the native terrain and
truss by `.1 m`, and has no invented common foundation. Platform, truss, and column boundaries
overlap but never reach the landing face.

Before columns subdivide it, every complete Warren-truss opening fits inside `3.1 by .75 m`, whose
diameter is `sqrt(10.1725)=3.1894356867634124 m < 3.2 m`; either end half-opening fits inside
`1.55 by .75 m`, whose diameter is `1.7219186970537043 m`. A lattice bay fits inside `1 by .8 m`,
whose diameter is `sqrt(1.64)=1.2806248474865698 m`. Because native samples are at least `10 m`
apart and clamped to `[.5,7.5]`, the terrain height can change by at most `.7 m` across one column
width; the small below-tie slope wedge therefore fits inside `1 by .7 m`, with diameter
`sqrt(1.49)=1.2206555615733703 m`. Added rails, ties, and braces can only subdivide these bounds,
and member stroke can only shrink them. The conservative truss and column colliders are therefore
honest: no complete `3.2 m` hull can enter an opening they reject while the narrower lattice remains
visibly open.

These are independently reconstructed conservative maxima, not a fixed inventory of opening counts.
For each site descriptor, the independent test implementation overlays all 14 truss members, all
variable column members, and the native terrain surface across each column; splits them at every
intersection; and enumerates each bounded connected clear face inside the truss/column collider
union. Every face must fit within one of the three raw centerline boxes above. It records the actual
maximum face's axis-aligned envelope width and height plus their `hypot` diameter, and proves that
diameter is no greater than `3.1894356867634124 m`. Column bay count and terrain feet may change the
face inventory, so neither production, fixtures, nor tests assign a fixed full/half-face count.

Static markup and dynamic rendering use byte-equivalent path/member attributes; model polygons,
independent world witnesses, and fixtures use the one truss envelope and three column envelopes.
Tests independently reconstruct the fixed truss plus every variable column segment, butt cap, round
join, stroked-pixel bound, terrain foot, lattice level, and the maximum connected clear-face
envelope instead of trusting class names or snapshots. A centered `H` and deck outline preserve the
elevated helicopter-pad reading, and no pale or white rectangle can appear below the platform when
play starts.

One gas can sits `3.0 m` right of platform center and does not collide. One NOC begins `2.0 m` right
of the platform edge. Its visual and collision lower bound is exactly `platformBottom`; native
terrain remains visible below it and the truss and columns carry it. Its solid `7.0 m`-wide
collision shell reaches the roof `7.2 m` above platform top. The NOC shell overlaps the top-chord
stroke and truss collider by `0.1 m`; that presentation decomposition is not a collision hole and
does not intersect the landing face. Its face contains one clean rectangular vertical battery
outline and four fill bars, with no terminal nub or rounded corner. A solid `0.5 m`-wide,
`3.2 m`-tall mast and antenna head rise from the roof. Three symmetric signal arches do not collide.

## 6. Projection, camera, and bounded retention

The SVG uses `viewBox="0 0 1000 640"`, `preserveAspectRatio="xMidYMid meet"`, and no intrinsic
minimum width. `#lander-scene-stage` is `position:relative`, has `aspect-ratio:25/16`, and owns that
SVG plus every overlay. `#lander-scene-shell` is a normal-flow column, `width:min(100%,60rem)`, and
contains the stage followed by the controls rail. It has no aspect ratio of its own. This keeps the
scene's exact projection while making the rail a real terrain-separated band rather than an overlay.
Neither box can cause page overflow at 320 CSS pixels or 400 percent zoom. Horizontal scale is
`10 scene units/m`; vertical projection is `sceneY=548-worldY*10`.

The controller computes one reversible dead-zone camera directly from the current immutable pose:

```text
cameraLeftForPose(pose) = pose.x < 5 ? pose.x-5 : pose.x > 35 ? pose.x-35 : 0
```

It writes `--camera-x=-cameraLeft*10px` on the game root. CSS applies one transform on
`#lander-world`:

```text
transform: translate(var(--camera-x), 0)
```

All terrain, sites, lander, agent, and debris retain absolute world-derived scene coordinates inside
that group. The camera holds the opening viewport while the reference point remains in `[5,35] m`,
keeps it at scene `x=50` during leftward travel and `x=350` during rightward travel, and is
continuous at both dead-zone boundaries. It has no origin clamp, monotonic furthest-X value,
horizontal extent, or controller cache. Contact, service, crash, checkpoint restoration, passing a
target, and returning from either direction always use the current frozen or restored pose.

The sky is a separate bounded decorative projection between `#scene-sky` and `#lander-world`. Define
`SKY_PARALLAX=.24`, `SKY_CHUNK_WIDTH=50 m`, `SKY_CHUNK_COUNT=5`, and `STARS_PER_SKY_CHUNK=4`. Let
`skyLeft=cameraLeft*.24`, `firstSkyChunk=floor(skyLeft/50)-1`, and retain exactly the five
consecutive indexes beginning there. Their `250 m` span always covers the `100 m` visible sky
interval plus at least one `50 m` buffer. The controller writes
`--sky-camera-x=-cameraLeft*10*.24px`; CSS translates only `#lander-sky-world` by that value. Stars
therefore move in the same direction as terrain at exactly 24 percent of its distance, producing
depth without autonomous animation.

For each retained sky chunk `c` and local star `i=0..3`, let unsigned `k=(Math.imul(c,4)+i)>>>0`,
`x=c*50+4+42*sampleUnit(seed,6,k)`, and `y=50+190*sampleUnit(seed,7,k)`. Append `M(x*10) y h2` to
the sole `#scene-stars` path in chunk then local-star order. Set
`landmarkOffset=floor(4*sampleUnit(seed,8,0))`. A chunk contains one landmark exactly when
`positiveModulo(c-landmarkOffset,4)===0`, so every retained five-chunk window contains one or two.
For such a chunk, use unsigned `q=c>>>0`, scene center `X=10*(c*50+10+30*sampleUnit(seed,9,q))` and
`Y=90+110*sampleUnit(seed,10,q)`, and select a crescent when `sampleUnit(seed,11,q)<.5`, otherwise a
ringed planet. A crescent is the two-arc outline
`M X (Y-18) A18 18 0 1 0 X (Y+18) A13 18 0 0 1 X (Y-18)`. A planet is the closed circle
`M (X-16) Y A16 16 0 1 0 (X+16) Y A16 16 0 1 0 (X-16) Y Z`. Set
`ringProfile=floor(3*sampleUnit(seed,12,q))`; profiles `0`, `1`, and `2` respectively use radii
`[(28,9)]`, `[(31,10)]`, and `[(28,9),(34,12)]`. Thus a planet has one compact ring, one slightly
wider ring, or two restrained concentric rings.

For every ring with radii `(rx,ry)` and planet radius `R=16`, derive the exact upper intersection
`cutX=sqrt((R^2-ry^2)/(1-ry^2/rx^2))` and `cutY=sqrt(R^2-cutX^2)`. Append only the exposed rear-side
segments and the complete foreground arc: `M (X-rx) Y A rx ry 0 0 1 (X-cutX) (Y-cutY)`,
`M (X+cutX) (Y-cutY) A rx ry 0 0 1 (X+rx) Y`, and `M (X+rx) Y A rx ry 0 0 1 (X-rx) Y`. The upper
center between the two circle intersections is not drawn because it lies behind the planet; the
foreground half remains visible across the planet. Full rear ellipses, quadratic fragments, more
than two rings, and radii outside the three profiles are forbidden. All landmark subpaths share the
sole `#scene-landmarks` path.

`skyProjectionForCamera(seed,cameraLeft)` returns the exact five-index key plus those two path
strings. Static no-JavaScript markup is its exact output for `STATIC_WORLD_SEED` and camera zero;
START and every sky-key change reconcile the same two nodes, while ordinary frames update only the
group transform. The group and both paths are permanently `aria-hidden`; they receive no title,
description, focus, pointer behavior, collision, model field, game state, request, storage, timer,
or event listener. `#scene-stars` retains its existing rounded graphite-gray stroke. Landmarks use
`fill="none"`, `stroke="#8a867c"`, `stroke-width="3"`, and round caps/joins. Hidden time freezes the
pose and both transforms; reduced motion changes no deterministic positional projection because the
sky has no independent motion. DOM stays exactly one group and two paths regardless of travel.

Terrain range projection never drops a segment merely because neither endpoint lies on a retained
`50 m` chunk boundary. Chunk indexes choose one closed retained range from the minimum retained
chunk edge through the maximum retained chunk edge. A pure helper emits the exact terrain height
interpolated from section 5.3's collision vertex chain at both range endpoints plus every interior
vertex, in strict X order. The result has one Y for each X, including exact chunk/site boundaries;
static and dynamic projections use the same helper output and never concatenate per-chunk paths.

Two SVG paths consume that one vertex array. `.terrain-fill` is the surface chain followed by the
two outer retained-range edges down to scene `y=648` and the floor edge, closed with `Z`; it has
`fill=#d7d2c4` and `stroke=none`. `.terrain-surface` is only the open surface chain, has
`fill=none`, `stroke=#4b4e55`, `stroke-width=2`, and `stroke-linejoin=round`, and contains no `Z` or
point at `y=648`. Therefore no fill closure can be painted, and the only stroked terrain path has
strictly increasing world X with no vertical segment. Mutation witnesses reject any same-X pair, any
different Y for a shared chunk/site boundary, a surface `V`, `Z`, or `L ... 648`, a fill stroke, or
more than this one fill/surface pair. This directly prevents the former visible vertical lines at
internal retained chunk boundaries, including during forward and backward reconciliation.

The visible interval is `[cameraLeft,cameraLeft+100]`. Retain chunks intersecting the interval plus
`40 m` on each side, at most five `50 m` chunks. Retain the active checkpoint site, target site, and
at most one immediately preceding powered site, at most three immutable site-state records. Terrain
generation uses only the retained chunk edges as its closed range. It receives the three small site
records so it can add site/column boundaries and apply either adjacent corridor rule only where that
range intersects them; it must never widen the terrain range to reach an offscreen site. The result
has at most 51 strict-X vertices and reconstructs identical native/corridor values from seed and
site descriptors after arbitrary eviction, reversal, and return. This closes the otherwise unbounded
gap between a far-away camera and a retained target while preserving collision terrain around the
vehicle. When a terrain or sky key changes, reconcile the fixed nodes once; ordinary frames update
transforms and attributes only. The run retains no discarded terrain or site history beyond
`completedSites` and the latest checkpoint snapshot.

Hard runtime ceilings are two terrain paths, three site groups, eight debris fragments, 80
descendants under `#lander-world`, one sky group with exactly two path descendants, 51 terrain
vertices, 64 queued input records, one pointer, one animation frame, and one pulse timer. The
lattice replaces members inside each existing `.site-scaffold` path and adds no descendant. The
simultaneous five-chunk-retention, three-site, eight-fragment projection therefore remains exactly
75 world descendants with five nodes of hard-budget margin. An installed agent reuses its site's
existing `.noc-entry` path, and the traveling agent keeps the existing global `#mission-agent`. The
fixed sky, scene-stage/outcome wrappers, and two native action descendants are outside
`#lander-world` and create no per-site or per-frame node. When enqueueing would create record 65,
discard all queued edges, sample the controller's complete keyboard, pointer, and sole
collective-pulse state, and enqueue exactly one `INPUT_SNAPSHOT` for the next integer
simulation-step boundary. The snapshot contains held physical codes; pointer-active, pointer ID,
pointer token, and anchor/current X; plus collective-pulse active, monotonically assigned token, and
deadline. Intermediate edges are deliberately lost; subsequent edges append after that record. This
is deterministic degradation, not an ordering-preservation claim. A 100-site browser witness must
keep these counts constant, show no increasing event-listener count, and keep active-game frame work
below 4 ms at the 95th percentile on the pre-merge Chromium machine. Direct template selection,
corridor construction, and exactly two proof replays together must finish below 25 ms at the 95th
percentile and 50 ms maximum over the same witness; record actual results rather than weakening the
ceiling.

Pure `targetDirectionForViewport(target,cameraLeft)` returns `"right"` exactly when
`target.platformLeft>cameraLeft+100`, `"left"` exactly when `target.platformRight<cameraLeft`, and
`null` otherwise. Equality means the platform has entered the viewport and hides the cue. The fixed
`44 by 44` solid right-arrow path uses transform `translate(932 280)` for right and
`translate(68 280) scale(-1 1)` for left, placing its mirrored paint at scene `x=[24,68]`; direction
never depends on blinking. The controller writes `data-target-direction="right|left|none"`, reveals
the visually hidden direction node on the same non-null predicate, and writes the corresponding
reviewed left/right sentence before adding its IDREF. Automated tests derive expected accessible
direction from the live predicate and current DOM text instead of embedding either authored
sentence. The cue initially points right after a service, reverses to left after the player passes
the target, and returns right if they cross back. It hides whenever the target platform is partly
visible. Reduced motion leaves the arrow static; hidden time pauses its existing blink.

## 7. Model shape, mission states, and checkpoint

### 7.1 Sole mutable run aggregate

`createRun({seed,reducedMotion=false})` returns a new aggregate with this conceptual shape:

```text
state, seed, missionSeconds, completedSites, refuelRatio
pose, commanded, fuel, fuelGaugeReference
generatorCursor, retainedChunks, retainedSites
activeSiteId, targetSiteId, targetRouteProof
touchdownPose, sequenceSeconds, refuel, agent, nocStage
checkpoint, launchStarted, launchCleared, failureCause, crashOrdinal, crash
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
unused fuel always carries forward. `fuelGaugeReference` is another model-owned positive number,
initialized to `30` while opening fuel is exactly `15`, then replaced only with the exact post-award
carried reserve at a safe contact. It never changes while that leg spends fuel and cannot affect
thrust, proof, award, collision, or fuel retention. The name must replace `legDepartureFuel`
throughout production, checkpoints, tests, browser probes, and permanent docs; retaining the old
field as an alias would preserve a lying second authority.

`refuel` is either null or the frozen exact record `{siteId,fromLevel,progress}`. Before safe
contact commits the award, `fromLevel` captures
`clamp(preAwardFuel/previousFuelGaugeReference,0,1)`, using zero when that denominator is zero. The
same transaction commits the real award, new `fuel`, and new `fuelGaugeReference`, then creates
`refuel` with the contacted site ID and `progress=0` only for normal motion. During `landed`, model
time sets `progress=clamp(sequenceSeconds/0.3,0,1)`. Crossing 300 ms clears `refuel` as the state
enters `deploying`; the ordinary gauge calculation is then exactly one. Reduced motion never creates
an intermediate record and projects the already-committed full result atomically. `refuel` changes
no fuel, route, award, checkpoint, collision, or world value.

`createPreflightModel()` and `START` set `refuel=null`. The only non-null interval is the
normal-motion `landed` presentation. Entering `deploying` or `failed`, generation error, `RESTART`,
Exit/destroy, and every new mission clear it.

Preflight uses the checked-in site 0 and initial pose but no active run seed or visible fuel. START
creates site 0 as target, `fuel=15`, `fuelGaugeReference=30`, `completedSites=0`,
`refuelRatio=ratio(1)=2`, and the initial approach:
`(x,y,vx,vy,angle,angularVelocity)=(30,32,0.8,-0.4,0,0)`.

The opening reserve has an independent, fixed-step feasibility witness for every possible first-site
deck. Starting from that exact pose with fuel `15`, command zero engines, then straight collective
`(.72,.72)`, then zero engines until first contact. For deck levels `83`, `91`, and `99`, the first
two run lengths are respectively `[396,108]`, `[396,96]`, and `[384,96]` steps. Representative seeds
`1`, `8`, and `13` select those three levels and land safely after `554`, `501`, and `512` total
steps with pre-award reserves `13.70399999999995`, `13.847999999999956`, and `13.847999999999956`.
The last clear poses are pinned in section 14. This test-only schedule is not a runtime assist,
planner, award input, or claim of minimum fuel. It proves the exact half-gauge opening remains
comfortably feasible under the real collision and landing profile.

### 7.2 State machine

The legal mission transitions are:

| From                     | Event                                   | To                       |
| ------------------------ | --------------------------------------- | ------------------------ |
| `preflight`              | `START`                                 | `flying`                 |
| `flying`                 | safe target-platform contact            | `landed`                 |
| `flying`                 | unsafe contact, overspeed, or ceiling   | `crashing`               |
| `landed`                 | `LANDING_SETTLED`                       | `deploying`              |
| `deploying`              | `AGENT_ENTERED`                         | `powering`               |
| `powering`               | `NOC_POWERED`                           | launch-ready `launching` |
| launch-ready `launching` | first effective collective command      | started `launching`      |
| started `launching`      | both feet clear deck by `0.05 m`        | `flying`                 |
| `crashing`               | `CRASH_COMPLETE`                        | `failed`                 |
| `failed`                 | `RESTART` with checkpoint               | `launching`              |
| `failed`                 | `RESTART` before first checkpoint       | `flying`                 |
| `flying`                 | catalog/proof invariant error           | `generation-error`       |
| Any active               | `EXIT`                                  | `preflight`              |
| `flying`                 | unsafe contact with reduced motion      | `failed`                 |
| `flying`                 | safe target contact with reduced motion | `launching`              |

The reduced-motion safe-contact transition atomically applies the complete `landed`, `deploying`,
and `powering` state result and stops launch-ready on the powered pad. There is no `succeeded`,
automatic launch, or terminal deployment state.

Safe contact performs one indivisible service preparation before `landed` renders: generate and
prove the next site, mark the contacted can collected, add its award, increment `completedSites`,
derive and store the next `refuelRatio` exactly once, and freeze upright at the deck. If next-site
generation hits its invariant error, none of these mutations commit and section 5.3's distinct
non-restartable error result applies. Normal timing is:

1. `landed`, 300 ms: settle, open the G bay, interpolate the gauge from the captured pre-award level
   to full, and project exactly one collected can from its site to that gauge.
2. `deploying`, 900 ms: the agent traverses linearly from the G-bay platform point to the NOC entry.
   Progress is exactly `sequenceSeconds/.9`, reaching `.25`, `.5`, `.75`, and `1` at 225, 450, 675,
   and 900 ms. It is visible for every time below 900 ms and hidden as the state enters `powering`
   at 900 ms. This halves only the Phase 4L pre-NOC travel duration.
3. `powering`, 1,400 ms: the vertical battery fills bottom-to-top at 200, 400, 600, and 800 ms. The
   inner, middle, and outer symmetric signal arches activate at 1,000, 1,200, and 1,400 ms. At the
   first 200 ms step, `nocStage` becomes `1` and the site's existing `.noc-entry` path becomes the
   installed-agent projection. It remains installed through stages 2-7 and whenever that retained
   site is `powered`, including launch, later legs, checkpoint restore, and Retry.
4. At `NOC_POWERED`, mark the NOC powered, create the checkpoint, set status exactly
   `Agent Deployed!`, and enter `launching` with both launch booleans false.
5. Launch-ready `launching` holds the centered pose, exact fuel, mission time, sequence time, and
   zero commanded engines indefinitely while the mixed raw request total is at most
   `TURN_DIFFERENTIAL=0.375` or post-fuel effective total is zero. Turn-only input therefore cannot
   rotate, burn, or release a restrained lander.
6. A request with raw total greater than `0.375` and nonzero post-fuel effective total sets
   `launchStarted=true`, clears the status, and integrates that same request in that same fixed
   step. There is no discarded first impulse. Every following step applies ordinary player input,
   gravity, fuel, and swept collision even if the player releases thrust before clearing the
   platform.
7. The active platform top is ignored only while `launchCleared=false` and the integrated velocity
   is upward. Once both transformed feet are strictly above `platformTop+0.05 m`, set
   `launchCleared=true` and return `flying` in that same step without resetting pose, fuel, command,
   clock, or input. Platform sides, underside, the unified truss, NOC, mast, and terrain are never
   ignored.

Reduced motion skips the 2,600 ms service presentation and atomically applies can collection, award,
the full gauge, agent entry/installation, all four battery bars, all three signal stages, powered
state, banner, and checkpoint. It emits no refuel record or transfer projection. It remains in the
same fuel-preserving launch-ready state and does not synthesize fixed-step launch input.

### 7.3 Immutable checkpoint

The model deep-copies and freezes this exact checkpoint after power and before launch:

```text
seed
completedSites
refuelRatio = ratio(completedSites+1), already advanced for the next base's award
generatorCursor
pose.x = platform center
pose.y = platform top
pose.vx = pose.vy = pose.angle = pose.angularVelocity = 0
fuel = exact post-award reserve
fuelGaugeReference = the same exact post-award reserve
activeSiteId
targetSiteId
targetRouteProof
retainedChunks = ordered chunk indexes
retainedSites = ordered descriptors with canCollected and powered flags
```

It excludes controller clocks, input, pointer, camera, refuel/transfer progress, debris, status
text, the pre-crash flight pose, and `crashOrdinal`. The ordinal is run-lifetime presentation
bookkeeping and survives checkpoint restore; it cannot affect physics, world, fuel, or awards. The
frozen `completedSites`, active and target IDs, and `refuelRatio` are the checkpoint's current
powered-base progress, next-base progress, and award authority. Restore copies them literally; it
does not iterate a recurrence or recompute them from mutable post-crash state. A model invariant
independently verifies `refuelRatio===refuelRatioForBase(completedSites+1)` at snapshot creation and
restore, so a mismatched snapshot fails rather than drifting.

`RESTART` clears the other excluded values, restores a fresh checkpoint copy, keeps the current run
seed, and enters launch-ready `launching` with `launchStarted=false`, `launchCleared=false`, zero
command, and status `Agent Deployed!`, with `refuel=null`. The restored vehicle remains on the last
powered pad without spending fuel until the player acts. Repeated Retry actions restore exactly the
same post-award fuel; they never recollect the can, add the award, advance `refuelRatio`, increment
progress, or repower the NOC. Before the first powered site, RESTART recreates the initial approach
with the same run seed, initial `fuel=15`, `fuelGaugeReference=30`, `completedSites=0`,
`refuelRatio=2`, `generatorCursor=1`, `activeSiteId=null`, `targetSiteId=0`,
`targetRouteProof=null`, and site 0's exact uncollected, power-off retained descriptor. Its pose is
exactly `(30,32,0.8,-0.4,0,0)`, with no service, checkpoint, status, or launch flags. The retained
window is reconstructed purely from that same seed and cursor; only `crashOrdinal` remains as
run-lifetime presentation bookkeeping. Exit or ordinary reload discards the checkpoint and gets a
fresh seed and zero crash ordinal.

The restore proof takes a canonical projection of the immutable checkpoint immediately after its
first creation and compares it with the post-Retry model after removing only the documented
launch-ready presentation fields. Equality is deep and exact, not approximate. It covers the same
active site and platform ID, centered upright six-component pose, carried post-award `fuel`, equal
`fuelGaugeReference`, target ID and complete `targetRouteProof`, `completedSites`, `refuelRatio`,
`generatorCursor`, ordered retained chunks, and every retained site's can, power, NOC stage, and
installed-agent-derived state. A second Retry after another crash must equal the same projection.
Separate counters around the transition prove no route proof runs, site is generated, can is
collected, award is added, base progress advances, or NOC is powered during either restore.

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
export const TURNING_TOTAL = 0.8;
export const MAX_THRUST_VECTOR = 30.0;
export const ANGULAR_ASSIST_DIFFERENTIAL = 0.12;
export const ANGULAR_ASSIST_FULL_SPEED = 15.0;
export const MAX_PLAYABLE_Y = 56.0;
export const MAX_LANDING_HORIZONTAL_SPEED = 2.2;
export const MAX_LANDING_DESCENT_SPEED = 3.6;
export const MAX_LANDING_ANGLE = 18.0;
export const MAX_LANDING_ANGULAR_SPEED = 26.0;
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
the explicitly fueled neutral-collective assist arrests rotation. Player flight, manual departure,
and reference schedules share `effectiveThrust` and `integrateStep`.

At upright full steering with collective, the exact axial acceleration is
`9*0.8*cos(30 degrees)=6.235382907247959`, or `48.112522432468824%` of straight collective's
`9*1.44=12.96`; lateral acceleration is `3.6`. At upright turn-only, axial acceleration is
`9*0.375*cos(30 degrees)=2.9228357377724805`, less than gravity `3`, while lateral acceleration is
`1.6875`. These independently computed values are acceptance bounds, not merely desired feel.

### 8.2 Input mixing and scheduler

Digital input first cancels opposing left/right controls, then selects one of these exact raw
requests. Space and Up are aliases for collective; Left/H and Right/L are aliases for steer.

| Collective | Steer      | Left    | Right   | Manual `s` |
| ---------- | ---------- | ------- | ------- | ---------- |
| off        | neutral    | `0`     | `0`     | `0`        |
| on         | neutral    | `.72`   | `.72`   | `0`        |
| off        | left       | `0`     | `.375`  | `-1`       |
| off        | right      | `.375`  | `0`     | `1`        |
| on         | left       | `.2125` | `.5875` | `-1`       |
| on         | right      | `.5875` | `.2125` | `1`        |
| off        | left+right | `0`     | `0`     | `0`        |
| on         | left+right | `.72`   | `.72`   | `0`        |

The steered collective rows have total `TURNING_TOTAL=0.80`, below straight collective `1.44`, and
difference `TURN_DIFFERENTIAL=0.375`. Turn-only rows have that same difference and total `0.375`.
Thus steering never obtains the former additive forward-thrust bonus.

Primary pointer down starts at equal `.72` thrust. For horizontal displacement, retain the existing
dead zone and full-bias distance and let signed normalized `bias` be in `[-1,1]`. Resolve input
sources as intents before producing an engine pair:

```text
keyboardSteer = -1 for left only, 1 for right only, otherwise 0
pointerSteer = bias while the pointer collective is active, otherwise 0
s = keyboardSteer != 0 ? keyboardSteer : pointerSteer
collectiveActive = keyboardCollective || pointerCollectiveActive || collectivePulseActive

if collectiveActive:
    base = 0.72 - 0.32*abs(s)
    halfDifference = 0.1875*s
    rawLeft = base + halfDifference
    rawRight = base - halfDifference
else:
    turnTotal = 0.375*abs(s)
    rawLeft = s > 0 ? turnTotal : 0
    rawRight = s < 0 ? turnTotal : 0
```

`pointerCollectiveActive` is true only during the captured primary pointer. `collectivePulseActive`
is section 11's one tokenized equal-thrust pulse, armed only by an eligible short tap. A nonzero
keyboard steer owns the signed manual steer even while either collective source is active; opposing
keyboard steers cancel and therefore allow an active pointer bias to own it. Pointer alone still
yields exactly `(.72,.72)`, `(.65375,.46625)`, and `(.5875,.2125)` at rightward bias `0`, `.5`, and
`1`, with leftward values mirrored. At full drag it yields the corresponding steered-collective row.

This arbitration produces one pair rather than combining engine components from independent pairs.
For collective input its total is exactly `1.44-0.64*abs(s)`, in `[0.80,1.44]`; without collective
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

When hidden, cancel the animation frame, pointer capture, collective-pulse timer, key state, and
active CSS animation; set `data-paused="true"`; and retain no accumulator. On visibility, the first
frame resets time without stepping. Hidden time advances neither physics, service, crash, cue, arrow
blink, nor mission time.

## 9. Swept collision and landing classification

Collision uses previous and next poses from every physics step. Broad phase includes nearby terrain,
the target platform, its full continuous truss envelope, all three column envelopes, every NOC body
and solid `0.5 m` mast, plus non-target structures. The model derives these closed polygons from
section 5.3's site descriptor, never from controller DOM coordinates. Interpolate position linearly
and angle by the shortest arc. Set `COLLISION_MARGIN=0.02 m`, hull radius `R=hypot(1.6,6.5)`, and
`travel=hypot(dx,dy)+R*abs(deltaAngleRadians)`. Use `N=ceil(travel/COLLISION_MARGIN)` equal-time
intervals, at least one and at most 64; a larger N is unsafe `overspeed`.

The scaffold broad-phase polygons conservatively cover the exact truss and three column envelopes
rather than pretending each narrow member is a separate passable collider. Section 5.3's
member-width, native-foot, and aperture-diameter proofs are the required honesty conditions for that
conservative treatment. Tests independently reconstruct every rendered member segment, clear
aperture envelope, and closed collision polygon; apply the exact butt-cap/round-join stroke
geometry; reject any rendered point outside its `+/-MEMBER_HALF` expansion; and reject any aperture
diameter at or above the rigid hull width.

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
and the complete truss polygon remain margin-expanded unsafe geometry. Only a bracketed true top
crossing proceeds to the safe-envelope test below, preserving ordinary pad landings without an early
top hit.

Classify the earliest contact, with unchanged equal-time precedence: NOC or mast, non-top platform
surface, unified truss or any column, terrain, then target platform top. The target top is safe only
when both transformed feet are on its closed `9.6 m` span, neither hull side intersects an end,
`vy<=0`, and these inclusive limits hold at contact:

```text
abs(vy) <= 3.6
abs(vx) <= 2.2
abs(normalizeDegrees(angle)) <= 18.0
abs(angularVelocity) <= 26.0
```

The boundary is inclusive and independently witnessed. A combined contact at `vx=2.2`, `vy=-3.6`,
`angle=-18`, and `angularVelocity=26` is safe. Four otherwise identical contacts that increase
exactly one magnitude by `1e-9` are unsafe. Mirrored signs independently prove the absolute-value
branches for horizontal speed, angle, and angular speed; any upward contact with `vy>0` remains
unsafe regardless of its magnitude.

After classifying the raw angled contact, settle upright at the exact platform center: set reference
`x` to the platform center, set reference `y` to the platform top, set angle to zero, and set both
linear velocities and angular velocity to zero. Both legal margin contacts therefore normalize to
the same deterministic settled pose before the route proof, award, or checkpoint commits. This makes
both transformed feet remain on the closed deck span and makes the later player-reachable launch
prefix use the pose that the route catalog actually proves. Gas-can art and antenna signal arcs do
not collide. Contact with a consumed/powered site's platform during a later leg is unsafe; only the
current target can complete a leg.

There is no horizontal mission boundary. Delete the active-site `center-45`, target-site
`center+65`, initial `-5`, fallback `101`, and every generic horizontal `"bounds"` failure path.
Crossing any former value, passing a target without touching it, reversing, and returning are
ordinary flight and do not complete, fail, or mutate site progress. The vertical ceiling remains
`pose.y>MAX_PLAYABLE_Y=56` and enters the crash sequence with the distinct cause `"ceiling"` after
an earlier swept contact has had precedence. `classifySweptContact` may still produce the existing
`"overspeed"` vehicle-safety failure when one fixed step exceeds its proven 64-subinterval sweep;
that is not a location bound. At zero fuel the effective engines and vector angle remain zero,
gravity continues, and only subsequent real terrain, structure, target-envelope, grazing, overspeed,
or ceiling classification can crash. Section 6's bounded local terrain projection covers the
complete swept region allowed by the overspeed ceiling even when every retained site is far
offscreen, so exploration cannot turn missing render history into fall-through or an artificial
horizontal failure.

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
most `24*120=2,880` fixed steps, and the first run is the player-reachable launch prefix `[1,90]`:
hold Space for exactly 90 fixed steps from launch-ready. The first effective step releases the pad
hold, and the remaining held steps continue through the ordinary `launching` to `flying` boundary.
The schedule stops at its first target contact. Clearance knots include both corridor endpoints,
have strictly increasing relative X, and linearly define the upper terrain envelope used in section
5.3. The stored successful contact satisfies every inclusive landing limit. The smaller allowance's
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
| 4     | `0.2125`    | `0.5875`     | Space + Left or H        |
| 5     | `0.5875`    | `0.2125`     | Space + Right or L       |
| 6     | `0`         | `0`          | Left/H + Right/L         |
| 7     | `0.72`      | `0.72`       | Space + Left/H + Right/L |

Rows store pre-assist requests. Replay derives manual `s`, the gimbal angle, any neutral-collective
assist, effective fuel scaling, force, and torque from each current pose exactly as section 8.1.
Duplicate physical-state rows remain because the table enumerates all eight keyboard combinations;
the derivation ordering treats the lower identical command index as canonical.

### 10.2 Independent derivation tool

Keep `website/tools/derive_lander_routes.mjs` and its pure sibling
`website/tools/lander_clear_faces.mjs` permanently. The deriver uses Node built-ins plus that
sibling only; neither tool imports production or test code, and runtime, model, and tests must not
import either tool. The sibling independently splits the rendered-member and terrain overlay into
bounded faces and returns the maximum connected clear-face witness; it owns no route,
world-generation, or fixture literals. Version `agw-lander-route-deriver/v5` with recipes
`agw-lander-route-recipes/v3` independently implements sections 5.2-5.3, 8, 9, and the reachable
command table, including the motif-bank traversal, true gimbal force, and neutral-collective assist.
Its versioned per-template constructive recipes give command phase order and finite integer step
ranges. Export `MAX_RECIPE_COMBINATIONS=256`. Each final recipe's explicit range Cartesian product
contains exactly four lexicographically ordered combinations. `256` per template and `2,304` over
nine templates are hard ceilings, not declared or evaluated family sizes. The complete ordinary run
evaluates exactly `9*4=36` candidates. The derived fixture pins each route's exact integer
`combinationsEvaluated=4`; that value must equal the recipe's independently recomputed
Cartesian-product size rather than a loop counter chosen after success. Derivation evaluates the
whole declared family, records that literal, and chooses by `(burn,totalSteps,RLE lexicographic)`,
failing rather than emitting an incomplete route. Every candidate must be safe in all nine pinned
seed/translation combinations per template. There is no early-success exit, undisclosed candidate,
beam, search heuristic, random retry, envelope relaxation, or runtime fallback.

Every tool and production replay begins at the same launch-ready centered pose with both launch
booleans false. It rejects a schedule whose first request total is at most `.375`, applies the first
qualifying request without a discarded step, and uses the same origin-top exception until both feet
clear by `.05 m`. The replay then proceeds continuously across the `launching` to `flying`
transition. This makes `[1,90]` proof of player-reachable departure, not a hidden automatic impulse.

The exact invocation is:

```text
node website/tools/derive_lander_routes.mjs \
  --geometry website/tests/fixtures/lander-route-geometry-v4.json \
  --output PATH [--verify PATH]
```

Unknown/missing flags exit 2; derivation or verification failure exits 1; success exits 0. Both
ordinary generation and ordinary `--verify` enumerate those same bounded families for all nine
templates. Verification then replays every selected winner and its one-quantum-smaller failure
witness across all nine pinned seed/translation combinations before comparing canonical output bytes
with the checked fixture; it has no theoretical two-million-candidate path or unchecked fast mode.
Thus a routine verification invocation evaluates exactly 36 candidates plus 162 selected replays;
the larger ceilings remain guards only. This is the required local/CI workflow, not a special
release-only regeneration job. `--geometry` contains schema `agw-lander-route-geometry/v4`, the nine
IDs, deltas, and literal clearance knots, plus one `siteGeometry` object with the exact platform
width, thickness, `2.4 m` clearance, exact deck levels `[8.3,9.1,9.9]`, `7.0 m` NOC width, `7.2 m`
roof offset, `0.5 by 3.2 m` mast, `0.2 m` member width, butt cap, and round join. Its `truss` object
pins `span=18.6`, `bayCount=12`, `bayWidth=1.55`, `bayHeight=.75`, `chordCount=2`,
`diagonalsPerBay=1`, `alternation="top-left-to-bottom-right-first"`, the conservative raw clear-face
maximum `width=3.1`, `height=.75`, and `diameter=3.1894356867634124`, and collision envelope
`[-4.9,13.9] x [-1.2,-.25]`. Its sibling `supportColumns` object pins count `3`, rail-pair offsets
`[[0,1],[8.8,9.8],[17.6,18.6]]`, rail/tie/brace width `.2`, maximum bay height `.8`, first-brace
alternation `"top-left-to-bottom-right-first"`, independently interpolated rail feet, bottom-tie
rule `max-rail-foot`, collision expansion `.1`, lattice maximum `1 by .8 m`, and terrain-wedge
maximum `1 by .7 m`. It contains no `pylons`, target-band, shelf, connector-width, regional
scaffold, post-grid, or platform/connector/NOC underframe fields. This incompatible field and
member-shape change advances both fixture filenames and schemas from v3 to v4; the v3 pair is
deleted in the same atomic commit with no reader, alias, fallback, or compatibility shim. Output
schema `agw-lander-route-derived/v4` contains `deriverVersion`, `recipeVersion`,
`canonicalPoseDecimals:9`, exact per-route `combinationsEvaluated`, `physicsDigest`,
`geometryDigest`, the ordered route records from section 10.1, `worldWitnesses`, `worldDigest`, and
`outputDigest`. `worldWitnesses` contains exactly 81 independently reconstructed world descriptors:
nine templates times three pinned seeds times three translations. Nesting is template outermost in
section 10.1 order, then seed in exact order `[11,39,41]`, then origin center in exact order
`[36,117,-42]`. For each template, the witness origin deck level is `83` for delta `+1.6`, `91` for
delta `+.8`, and `99` otherwise; target level is the exact integer sum with the template's decimeter
delta. The serialized flat array follows that nested order without sorting or regrouping.
Independent feasibility enumeration proves both closed site footprints satisfy their local native
minimum in all 81 cases; changing a seed, center, level, or comparison must fail rather than
silently raise a platform. Each descriptor includes the selected motif-bank offset, direction, and
relevant per-chunk indexes; the one strict-X terrain array with `10 m`, site-boundary, and six
column-rail-foot samples; open-corridor cap relief; both platform decks; the fixed 14 truss members
plus every bounded variable lattice member with cap and join semantics; the raw-truss, lattice-bay,
terrain-wedge, and actual maximum connected clear-face bounds; the one truss and three column
conservative collision envelopes; NOC bodies, complete NOC collision envelopes, and mast colliders;
and its own digest. Canonical JSON recursively sorts object keys, preserves array order, uses
`JSON.stringify` without whitespace, and hashes UTF-8 bytes with lowercase SHA-256. `geometryDigest`
hashes the complete geometry object; `physicsDigest` hashes an object containing every named numeric
constant in sections 8-10, including gimbal and assist constants, plus the eight pre-assist command
rows; `worldDigest` hashes the ordered world descriptors; `outputDigest` hashes the output object
with only `outputDigest` omitted. The file adds one unhashed trailing LF.

Candidate search, fuel burn, collision classification, route ordering, demonstrated minima, and all
world/geometry values retain raw JavaScript numbers. Only after the winning route is selected, the
deriver canonicalizes each numeric component of its selected success and one-quantum-exhaustion pose
as `Number(value.toFixed(canonicalPoseDecimals))`. This bounds native `sin`/`cos` last-bit variation
across supported CPU architectures while remaining inside the production replay's existing `1e-9`
pose tolerance. It never changes a schedule, contact/exhaustion step, burn, safety decision, world
descriptor, geometry value, or geometry/physics/world digest. Tests collapse sub-precision pose
jitter, reject a precision change, reproduce the canonical bytes on x64 CI and the ARM64 pre-merge
host, and prove all strict world/geometry values remain untouched.

The reviewed output becomes `website/tests/fixtures/lander-route-derived-v4.json`. Phase 4M uses
deriver v5, unchanged recipes v3, geometry v4, and derived schema/file v4. Regeneration
independently applies section 5.3's six rail feet, variable lattice members, three column colliders,
and section 9's revised landing limits to all 36 candidates and then all 162 selected
success/failure replays. The intended result is byte-identical schedules, schedule digests,
demonstrated minima, success and one-quantum-failure vectors, and `combinationsEvaluated=4` values.
That equality is evidence to establish, not an assumption: if any widened column changes a selected
collision or makes a recipe family infeasible, implementation stops with the exact template, seed,
translation, member, collision, candidate count, and replay witness. It must not weaken a column,
collider, clearance knot, terrain, landing limit, or proof bound to retain an old route.

The final geometry digest is `a5120d97782b73afb43cabae038412252f644656f41c0ab9e33f5413da9be7ca` and
the world digest is `c666bb42918301f93386bb1373e92da662d333006d8684946fd80a10761d1e32`. The final
tolerance bump changes only the physics digest to
`e08f8260b723dd245db88de9ae2cdbac54bf9a97cb0bed1b6f170eda362c48dc` and the consequent output digest
to `a922372760f850386810fd6eb60f7aa807bac8b03ee5f0a2b1dec1968ee27b69`. Ordinary generation and
verification prove all 81 world descriptors and all nine route/failure records remain
byte-identical; the antenna and sky projections are outside route derivation. A missing expected
change, a geometry or world change, a route-literal change, or a partial digest update is a hard
failure.

Regenerate the complete v4 geometry and derived fixtures and compare production route/failure
literals and digests in one atomic change. Tests project production arrays back to the two schemas,
compare canonical bytes with both fixtures, and recompute all digests before replay. Independent
test-side reconstruction compares all 81 motif selections, strict-X native/corridor arrays,
deck-level/truss/column/NOC descriptors, inserted native boundary and six-foot values, lattice
levels, variable members, colliders, the independently enumerated maximum connected clear-face
envelope, the three conservative aperture maxima, and corridor samples to production with strict
numeric equality and pinned ULP-sensitive vectors. Thus the world and tool consume identical
envelope values while independently implementing corridor construction, physics, assist, gimbal, and
collision. Intentional regeneration writes to temporary paths, reviews the canonical mismatch,
atomically deletes both v3 fixtures and installs both v4 fixtures, updates all four production
`ROUTE_DIGESTS`, proves route/failure literals equal before retaining them, and updates independent
expected tests. It then runs ordinary `--verify` against the checked v4 fixture. Recipe v3 remains
unchanged. No old fixture, schema reader, or dual authority survives. Never weaken geometry or proof
bounds merely to make a recipe pass. Ordinary tests only verify checked data and never regenerate
expectations. `website/README.md` permanently teaches this workflow. Neither fixture nor tool enters
the 12-file artifact.

Catalog tests replay every literal from an upright origin with `fuel=demonstratedMinimum`, using the
exact production fixed-step gimbal/assist physics and translated strict-X native/corridor geometry.
Each must land at its literal success vector. A second replay with
`fuel=demonstratedMinimum-FUEL_QUANTUM` must match its literal fuel-exhaustion witness before target
contact. Exhaustively validate all nine entries and command indexes. This establishes a conservative
demonstrated minimum at the catalog's fixed schedule and fuel resolution; it makes no claim about a
lower-fuel schedule or a global physical optimum.

At site creation, direct selection translates the chosen literal geometry and constructs a
provisional checkpoint identical to the future real checkpoint except for fuel and the proof being
formed. It includes seed, the centered upright settled origin pose, completed count, next refuel
ratio, generator cursor, site IDs, retained descriptors, collected current can, and powered current
NOC. For each of exactly two defensive replays, replace only fuel: first with the literal
demonstrated minimum, then with one quantum less. Neither replay reads carried reserve or refuel
ratio. The first must reproduce success and the second its checked-in failure. Any mismatch takes
the defensive `generation-error` path; there is no runtime search, optimization, descent, retry, or
alternate-template loop. If any of the nine reviewed templates has no safe derivation inside its
finite v3 recipe and the 256-combination per-template ceiling, implementation stops and reports the
exact exhausted ranges, exact evaluated count, and collision/fuel witnesses. It must not enlarge a
recipe beyond 256, ship partial catalog data, widen the safe envelope beyond section 9, lower
terrain, add a third runtime replay, or substitute runtime search.

A design probe applied the Phase 4I `0.8` total, `30 degree` vector, and `2.4 m` clearance to the
old v2 recipes without changing their ranges. As expected, `route-78-flat` exhausted all 81 old
candidates without contact; the first old candidate ended after 2,335 steps at relative
`(x,y)=(71.20621062728333,23.345187498493207)` with
`(vx,vy)=(-2.737007272427576,-0.5102185054586155)`. This proves that reusing the Phase 4H recipes is
invalid; it is not evidence that the finite v3 constructive family is infeasible. Implementation
must produce all nine new proofs or stop with the required exhaustion evidence. It may not silently
restore old force or structure values. Here "v2 recipes" names the retired
`agw-lander-route-recipes/v2` family only; the independent Phase 4L probe above uses the required
`agw-lander-route-geometry/v3` schema and current v3 recipes.

### 10.3 Demonstrated minimum and award

Set `FUEL_QUANTUM=0.05`; every catalog minimum is greater than one quantum and is an exact integer
multiple of it. Independent derivation sets it to
`ceil(exact successful schedule burn/FUEL_QUANTUM)*FUEL_QUANTUM`; the next lower quantum exhausts
before contact. Store the template ID, successful replay, smaller-failure replay, quantum, literal
schedule digest, and burn in `targetRouteProof`. The runtime always performs exactly those two
replays and no fuel scan. The proof outcome and demonstrated minimum are therefore independent of
carried excess and of the stored refuel ratio.

For one-indexed successfully powered base number `n`, define the exact term **refuel ratio** by
`ratio(n)=1+0.5^(n-1)`. Base `n=1` is the first base the player safely lands on and powers, so the
exact mathematical sequence begins `2, 1.5, 1.25, 1.125, 1.0625, ...` and approaches `1` from above.
Runtime computes each term directly from the base ordinal with the exact JavaScript Number
expression below. It is deterministic O(1); never advance by multiplying or subtracting from the
prior ratio:

```js
export function refuelRatioForBase(baseNumber) {
  return 1 + 0.5 ** (baseNumber - 1);
}

const poweredBaseNumber = model.completedSites + 1;
const ratio = refuelRatioForBase(poweredBaseNumber);
if (model.refuelRatio !== ratio) invariantError();
const award = demonstratedMinimum * ratio;
const nextRatio = refuelRatioForBase(poweredBaseNumber + 1);
```

The model's existing progress invariant supplies integer `baseNumber>=1`; the ratio function adds no
arbitrary-precision representation, epsilon floor, cap, or artificial base-count bound. IEEE-754
rounding is part of the contract: `ratio(52)=1.0000000000000004`, `ratio(53)=1.0000000000000002`,
`ratio(54)=1`, and `ratio(100)=1`. The runtime sequence is non-increasing and never below `1`; it
rounds exactly to `1` beginning at `n=54`, even though the mathematical sequence remains strictly
above `1` and approaches it asymptotically.

START stores `refuelRatioForBase(1)=2`. On safe contact for base `n`, validate the stored ratio,
compute `award=demonstratedMinimum*refuelRatio`, add that award to carried excess without rounding
or a capacity clamp, increment `completedSites` from `n-1` to `n`, store `ratio(n+1)` for the next
base, and assign the exact resulting reserve to `fuelGaugeReference`. The transaction is atomic.
With a test minimum of `8`, the first four ratio/award/next-ratio vectors are exactly
`(1,2,16,1.5)`, `(2,1.5,12,1.25)`, `(3,1.25,10,1.125)`, and `(4,1.125,9,1.0625)`. If carried fuel at
those contacts is respectively `7,20,5,4`, the exact post-award reserves are `23,32,15,13`, proving
the award is added rather than replacing carried excess. Display the current reserve to one decimal
place without changing either model value. The award path does constant work and never loops over
`completedSites`.

The contacted site's gas can is consumed only after the proof succeeds. The award is based on the
new target, so site 0's can funds leg 1. Initial fuel funds the approach to site 0. After the NOC is
powered, replace provisional trial fuel with `carriedFuelAtContact + award`, attach the proof, and
freeze the real checkpoint with `completedSites=n` and `refuelRatio=nextRatio`. Proofs are
byte-identical across different carried reserves or ratios because neither is a proof input. With
the same current ratio, different carried reserves also produce the same award; changing the ratio
changes only the award, never the proof. The player-commanded departure spends from real checkpoint
fuel in actual play. Carried excess can therefore compensate for a later flight that uses more than
the reference route.

## 11. Input, focus, and lifecycle

Match Space/arrows by `code` and `h`, `l`, `r` by lowercase `key`. Preflight starts only for a
non-repeated, unmodified Space targeted at body, game, shell, or scene. Exclude anchors, buttons,
form controls, editable elements, and descendants. The accepted key starts and seeds held Space at
the same timestamp. Native Start activation starts without thrust.

Every active keydown and keyup first validates that the event belongs to the active shell path.
`activeShellEventPath(event)` uses `event.composedPath()` when callable and nonempty, otherwise
walks `event.target` through its Element ancestors, and accepts only when that path contains the
exact current `#lander-scene-shell` while the mission is active. If this first gate fails, return
before `preventDefault()`, repeat or key classification, focus movement,
held-key/token/queue/pulse/thrust mutation, or model dispatch. Outside-shell Escape, `r`, Space/Up,
arrows, `h`/`l`, and their keyup events therefore remain page input and have zero game effect.

Within an accepted active-shell keydown, arbitrate in this exact order: unmodified Escape dispatches
EXIT; unmodified `r` dispatches RESTART only in `failed`; then interactive/editable rejection runs;
only then may `flying` or `launching` accept a physical flight key. Define `INTERACTIVE_KEY_TARGET`
as the exact selector
`a[href],button,input,select,textarea,summary,[contenteditable]:not([contenteditable="false"]),`
`[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="switch"],[role="menuitem"],`
`[role="option"],[role="slider"],[role="spinbutton"],[role="textbox"],`
`[tabindex]:not([tabindex="-1"]):not(#lander-scene-shell)`. `isInteractiveKeyEvent(event)` inspects
`event.composedPath()` when it is callable and nonempty, otherwise `[event.target]`, and returns
true when any Element in that path has a non-null `closest(INTERACTIVE_KEY_TARGET)`. This catches
Exit, Retry, and either button's nested label or hint span, plus every other interactive/editable
descendant that could appear later. For any keydown rejected by this predicate, return before
`preventDefault()`, repeat consumption, held-key mutation, input-token allocation, queue mutation,
pulse/thrust mutation, focus movement, or model dispatch. Space and Enter therefore retain only the
focused native button's browser activation; arrows and `h`/`l` retain their ordinary control
behavior and do nothing to the mission.

After those gates, `flying` and `launching` accept flight keys on the non-interactive active shell
path. Launch-ready mixing still applies section 7.2's collective threshold, so steer-only keys
remain queued physical state without moving or burning the restrained lander. Consume repeats but
enqueue only first edges and track aliases by physical code. Record which physical keydown events
were accepted. An in-shell keyup enqueues a release only for a matching previously accepted physical
key; a keydown rejected on an interactive path is never recorded, so its later keyup returns before
`preventDefault()` and every input mutation. Shell `focusout` clears accepted input before focus can
move outside the shell, where the first gate makes every later key event a no-op. Window blur, hide,
exit, Retry, contact, and destroy use the same held-input teardown.

There is no native Launch action or special departure handler. Launch-ready departure uses the
ordinary active-game input paths: Space, Up, Space or Up combined with H/L or Left/Right, a captured
primary pointer/touch hold, or an eligible short tap. The first mixed request whose raw total is
greater than `.375` and whose post-fuel effective total is nonzero crosses the model's existing
threshold, spends fuel, and integrates in that same fixed step. H/L or Left/Right without collective
remains the `.375` restrained turn-only request and cannot burn, rotate, or depart. Implementation
deletes `#lander-launch` from the shared fragment, controller fields and lookup, listener
registration and teardown, activation handler, fake-DOM builders, validator required-ID and
action-order authority, CSS selectors, and browser/unit fixtures. It is not retained hidden,
disabled, detached, or synthesized. No `launch-button` source remains in the queue, pulse snapshot,
or tests; `launching` remains the model state name for the restrained launch-ready lifecycle only.

Pointer listeners and capture remain on the existing `#lander-scene-stage` boundary, so the new
normal-flow rail can never start, sustain, or cancel flight input. Phase 4L preserves that browser
event boundary and changes neither pointer mixing nor model input. Define
`INTERACTIVE_POINTER_TARGET` as the exact selector
`a[href],button,input,select,textarea,summary,[contenteditable]:not([contenteditable="false"])` and
`isInteractivePointerEvent(event)` as
`event.composedPath().some(node => node instanceof Element && node.closest(INTERACTIVE_POINTER_TARGET))`.
Every stage pointer handler returns immediately when that predicate is true. In particular,
`pointerdown` does so before `preventDefault()`, token allocation, capture, or enqueue. This
necessarily rejects Start and Retry, including a descendant as the original target, while leaving
each native activation handler authoritative. In `flying` or `launching`, an otherwise eligible
primary pointer button 0 on the stage captures one pointer and activates pointer collective at equal
`.72` thrust. Horizontal travel produces section 8.2's pointer steer intent from:

Exit is outside the stage in the controls rail, so its pointer events cannot bubble to any stage
handler. Retry is inside the stage outcome, so its descendant-composed path proves the rejection
guard executes before `preventDefault()`, token allocation, capture, or queue mutation. Native Retry
and Exit activation each runs exactly once through its own click handler.

```text
deadZone = max(10 px, scene width * 0.01)
fullBiasDistance = max(56 px, scene width * 0.18)
bias = sign(dx) * clamp((abs(dx)-deadZone)/(fullBiasDistance-deadZone),0,1)
m = abs(bias)
base = 0.72 - 0.32*m
pointerLeft = base + 0.1875*bias
pointerRight = base - 0.1875*bias
```

The last two values are the exact pointer-only result; they are not a second engine pair to merge.
Section 8.2 arbitrates the pointer intent with the keyboard intent before producing the sole raw
engine pair.

One monotonically increasing input-token namespace covers every accepted pointer down; a browser
`pointerId` is never a token. The sole collective-pulse aggregate is exactly
`{active,token,deadline}`. `beginCollectivePulse(token,startTimestamp,deadline)` atomically enqueues
the old pulse's zero edge if active, cancels its timer, stores the new aggregate, enqueues its one
equal-thrust edge at `startTimestamp`, and schedules the one token-and-deadline-checked end
callback. It mutates no model state directly.

Every accepted pointer down receives the next token. A release within 180 ms and 10 CSS pixels hands
that same token to `beginCollectivePulse`, starting at the pointer-up timestamp, and
`deadline=pointerDownTimestamp+140 ms`; the active pointer edge ends and pulse edge begins at that
same timestamp. The handler records a capture-release association `{pointerId,token}` before calling
`releasePointerCapture`. A lost-capture event resolves its token through that association, never by
matching a reusable `pointerId` alone.

Before accepting another pointer down, the helper's atomic supersession above ends any retained
pulse before the new input edge. Exactly one pointer, one collective pulse, and one pulse timer can
therefore exist. Every scheduled callback captures token and deadline and becomes a no-op unless
both still equal the current aggregate; an old callback can never clear, cancel, or enqueue against
a later gesture even when the browser reuses its `pointerId`.

The browser may synchronously dispatch `lostpointercapture` from that release. If its released token
equals the current pointer-tap pulse token and the deadline remains in the future, the handler
clears capture-only bookkeeping but deliberately leaves that pulse, timer, and queued input intact.
The timeout verifies the same token and deadline, enqueues the one pulse-end edge, and clears the
aggregate. If the 140 ms minimum has already elapsed at pointerup, completion is immediate.
`pointercancel`, a lost capture before eligible completion or for an unrelated token, stall discard,
contact, blur, hide, exit, Retry, and destroy invalidate the active pointer and pulse through the
same idempotent full teardown. A resulting second event is a no-op. Flight input is ignored during
service, crash, and failure, but launch-ready and started `launching` use the same input queue,
pointer lifecycle, and collective-pulse authority as flight. `touch-action:none` applies exactly to
`#lander-game[data-mission-state="flying"] #lander-scene-stage` and
`#lander-game[data-mission-state="launching"] #lander-scene-stage`. No rule assigns it to the shell,
controls rail, outcome, or native buttons; all other states and page surfaces retain
`touch-action:auto`, scrolling, zoom, text selection, and link behavior.

`destroy()` cancels the frame, listeners, media-query listener, capture, pulse timer, active ARIA,
status, and thrust; it hides and disables Retry and Exit, hides the controls rail, and leaves static
recovery markup intact. Never intercept Tab or trap focus. The header and breadcrumb remain
available in every state, and the existing `#lander-status` remains the only live region.

## 12. Plumes, direction cue, NOC, and reduced motion

`plumeForThrust(u)` returns `scaleY=0.08+0.92*u` and `opacity=0.25+0.75*u`. The controller also
projects `commanded.vectorAngle` as `--thrust-vector-angle`: section 8.1's exact
`MAX_THRUST_VECTOR*s` while effective thrust is nonzero and zero otherwise. CSS independently scales
the external engine uses and rotates both force axes by that signed angle around their respective
`(82,401)` and `(158,401)` engine anchors. Neutral-collective assistance leaves this angle at zero
but produces visibly different plume lengths from its post-assist effective engines. Plumes affect
no collision or layout.

Scene tokens remain local and fixed: sky `#f5f2e8`, stars `#8a867c`, terrain `#d7d2c4`, platform
fill and scaffold member `#4b4e55`, NOC shell `#20232a`, inactive battery `#3b3f47`, battery stages
bottom-to-top `#d94a1e`, `#ff7a00`, `#ffe09a`, and `#7de2c5`, signal stages `#d94a1e`, `#ff7a00`,
and `#7de2c5`, gas can `#d94a1e`, and helipad marking `#f5f2e8`. Arcade fuel levels add danger red
`#ff5a36`, caution amber `#ffb000`, and ready green `#2ed49b`. Their contrast ratios against the
dark track `#20232a` are respectively `5.068:1`, `8.584:1`, and `8.243:1`, all above `3:1`. Graphite
`#292b30` remains the gauge's outer component boundary against the sky. Shape, outline, fill
progression, gauge height, explicit outcome text, and the solid `H` keep structure, fuel, battery,
and direction meaning independent of color.

Activated chrome uses only this local-system stack:

```css
font-family:
  ui-monospace, "Cascadia Mono", "Segoe UI Mono", "Liberation Mono", Menlo, Monaco, Consolas,
  monospace;
font-weight: 700;
letter-spacing: 0.025em;
```

Apply it only to visible fuel chrome, outcome/Retry, and controls-rail chrome. Crisp three-pixel
borders, square corners, and a `3px 3px 0 #292b30` block shadow provide the arcade treatment. Do not
use `@font-face`, `@import`, `url()`, remote/local font probing, generated text, text
transformation, or a font-dependent glyph. Existing CSP, asset manifest, and runtime request rules
remain byte-for-byte unchanged.

`#lander-game` is the sole carrier of mission-wide state. Keyed `.lander-site` groups are
projections of model-owned per-site state, not independent authorities. The controller writes:

- `data-mission-state` from section 7 and `data-launch-ready="true|false"` from
  `state==="launching" && !launchStarted`;
- `data-paused`, `data-cue`, `data-target-offscreen`, and `data-reduced-motion`;
- `data-fuel-level="empty|danger|caution|ready"`, `data-refueling="true|false"`, and
  `data-banner="none|deployed|crashed|error"` from the exact projections below;
- `data-can="present|collected"`, `data-power="off|on"`, `data-agent="absent|installed"`, and
  `data-noc-stage="0|1|2|3|4|5|6|7"` on each retained keyed site group;
- custom properties `--camera-x`, `--lander-x`, `--lander-y`, `--lander-angle`, independent plume
  scale/opacity, `--thrust-vector-angle`, `--agent-x`, `--agent-y`, `--crash-x`, `--crash-y`, and
  `--crash-progress`; unitless `--fuel-gauge-level` and `--refuel-progress`; and pixel lengths
  `--fuel-transfer-x` and `--fuel-transfer-y` relative to `#lander-scene-stage`.

The only CSS keyframes are `agw-preflight-cue`, `agw-target-cue`, and `agw-fuel-empty-blink`.
Preflight runs three subtle plume pulses over 2.4 seconds once per document load; reduced motion and
START settle immediately. The target arrow blinks at a 900 ms period only while its solid
right-pointing shape is visible, motion is allowed, and the document is active. Reduced motion
leaves it continuously visible. Exact empty fuel flashes the entire red gauge at a 700 ms stepped
period between opacity `1` and `.3`; reduced motion keeps the same whole-gauge red warning without
animation. `data-paused="true"` pauses all three keyframes. Transfer, banner, and installed-agent
projection use no keyframe, CSS transition, timer, or completion event.

Pure `fuelGaugeLevel(model)` first computes the ordinary authoritative level as
`clamp(model.fuel/model.fuelGaugeReference,0,1)` when `fuelGaugeReference>0`, otherwise zero. If
`model.refuel` exists, it instead returns
`model.refuel.fromLevel+(1-model.refuel.fromLevel)*model.refuel.progress`; otherwise it returns the
ordinary level. The controller assigns that result to `--fuel-gauge-level` and the exact progress to
`--refuel-progress`. `empty` is exactly `model.fuel===0`; for positive authoritative fuel, `danger`
is `0<=level<=0.2`, `caution` is `0.2<level<=0.5`, and `ready` is `0.5<level<=1`. Thus an atomic
positive award with a still-zero refuel presentation is danger, not empty. The controller writes
exactly one of those values to `data-fuel-level`; `empty` and `danger` map to the same exact red
`--fuel-level-color`, while the other bands retain their exact colors. Fill height uses
`scaleY(level)`. Independently, the same level color remains visible as the track's three-pixel
inset indicator even when height is zero. At `empty`, the outer border and background also become
danger red, the inset becomes dark graphite, and the whole track runs
`agw-fuel-empty-blink 700ms steps(1,end) infinite`. Nonempty bands retain the graphite outer
boundary, so component boundary, level color, and bottom-up amount remain three distinct visual
signals; none is an accessible semantic meter.

When visible, `#lander-fuel` is a pointer-transparent block positioned inside `#lander-scene-stage`
at `left:clamp(0.5rem,2vw,1rem)` and `top:clamp(0.5rem,2vw,1rem)`. The track is exactly `1rem` wide
by `7rem` tall with `box-sizing:border-box`, a three-pixel graphite outer border, dark `#20232a`
background, square corners, the exact `inset 0 0 0 3px var(--fuel-level-color)` indicator, and the
pinned outer block shadow. Its child occupies the inner track, uses
`background:var(--fuel-level-color)`, and uses `transform:scaleY(var(--fuel-gauge-level))` with
bottom-center origin. The label and value span remain in the accessibility tree through
`.visually-hidden`; no CSS rule may use `display:none`, `visibility:hidden`, zero font size, or
`aria-hidden` on either. CSS must select `#lander-fuel:not([hidden])`, preserve the global
`[hidden]` authority, and avoid intercepting scene input.

For normal motion only, `data-refueling="true"` makes the sole `#lander-scene-stage::after`
pseudo-element visible as a small blocky gas can. It contains no text and creates no DOM or world
node. The SVG is `display:block;width:100%;height:100%` inside the exact `25/16` stage. Because that
ratio equals the `1000 by 640` viewBox, `preserveAspectRatio="xMidYMid meet"` has no contain
letterboxing: `scaleX=stageRect.width/1000` and `scaleY=stageRect.height/640` in the same
stage-local CSS-pixel frame.

At each render, read `stageRect=stage.getBoundingClientRect()` and
`gaugeRect=gauge.getBoundingClientRect()`, then calculate exactly:

```text
p = model.refuel.progress
sceneCanX = (site.center+3)*10-cameraLeft*10
sceneCanY = 548-(site.platformTop+1.5)*10
canViewportX = stageRect.left+sceneCanX*scaleX
canViewportY = stageRect.top+sceneCanY*scaleY
gaugeViewportX = gaugeRect.left+gaugeRect.width/2
gaugeViewportY = gaugeRect.top+gaugeRect.height/2
startX = canViewportX-stageRect.left
startY = canViewportY-stageRect.top
targetX = gaugeViewportX-stageRect.left
targetY = gaugeViewportY-stageRect.top
transferX = startX+(targetX-startX)*p
transferY = startY+(targetY-startY)*p
```

Write those last two stage-local lengths to `--fuel-transfer-x/y`. The pseudo-can uses
`left:var(--fuel-transfer-x);top:var(--fuel-transfer-y);transform:translate(-50%,-50%)`, so the
coordinates denote its center. Its exact CSS is:

```css
#lander-game[data-refueling="true"] #lander-scene-stage::after {
  content: "";
  position: absolute;
  inline-size: 20px;
  block-size: 22px;
  left: var(--fuel-transfer-x);
  top: var(--fuel-transfer-y);
  pointer-events: none;
  transform: translate(-50%, -50%);
  image-rendering: pixelated;
  background:
    linear-gradient(#d94a1e 0 0) 6px 2px / 6px 2px no-repeat,
    linear-gradient(#292b30 0 0) 4px 0 / 10px 6px no-repeat,
    linear-gradient(#d94a1e 0 0) 16px 10px / 2px 4px no-repeat,
    linear-gradient(#292b30 0 0) 16px 8px / 4px 8px no-repeat,
    linear-gradient(#d94a1e 0 0) 2px 6px / 12px 14px no-repeat,
    linear-gradient(#292b30 0 0) 0 4px / 16px 18px no-repeat;
  background-color: transparent;
}
```

Multiple CSS backgrounds paint first-listed on top. The six layers are therefore, in paint order,
orange handle void/fill over graphite handle, orange inner spout over graphite spout, and orange
inner body over graphite body. They reproduce the source can's exact `#292b30` graphite and
`#d94a1e` orange language in a recognizable sharp-cornered silhouette. The empty generated string is
not text, exposes no accessibility node or name, and `pointer-events:none` makes the projection
silent to input. No border, mask, filter, image asset, seventh layer, or nontransparent background
color participates.

At `p=0` the original collected can is already hidden and the pseudo-element occupies its anchor, so
exactly one can is visible. At 300 ms `refuel` clears, `data-refueling` becomes false, the
pseudo-element disappears at the gauge, and the fill remains exactly full. The controller's
registered resize handler reruns this projection from both current rectangles without changing `p`;
its cleanup is in the existing teardown registry. Reduced motion never exposes the pseudo-element. A
hidden document freezes model progress; resize may reproject the same frozen `p`, and the first
visible frame recomputes both rectangles without adding hidden elapsed time.

The existing `#lander-status` remains the sole status, live-region, and banner text authority.
`data-banner="deployed"` is derived only from the launch-ready model state; `crashed` only from
`failed`; `error` only from `generation-error`; and `none` otherwise. Banner selection never
branches on repository-authored status wording. For `deployed`, `crashed`, or `error`,
`#lander-outcome` is positioned exactly with `position:absolute`,
`inset-block-start:clamp(0.5rem,2vw,1rem)`, `inset-inline-start:50%`, `inset-inline-end:auto`,
`transform:translateX(-50%)`, and `inline-size:min(32rem,calc(100% - 6rem))`. It is a column aligned
to center. `#lander-status` is a centered three-pixel graphite-bordered, sky-backed arcade panel;
deployed uses `box-shadow:inset 0 -3px 0 #2ed49b,3px 3px 0 #292b30`, crashed substitutes `#ff5a36`,
and error retains only `3px 3px 0 #292b30`; the exact text independently names the outcome. Retry
follows status directly in DOM and visual flow with a `0.5rem` block-start margin. It is visible
only for `crashed`, where it appears beneath `Crashed!`; deployed and error banners contain no
action. With `data-banner="none"`, status uses the established visually hidden clipping recipe and
Retry stays hidden and disabled. The outcome is pointer-transparent except that the visible Retry
itself restores `pointer-events:auto`; its descendant events still reach the stage and are rejected
by section 11 before any flight-input effect.

`#lander-controls-rail` is the final child of `#lander-scene-shell`, immediately after the stage. It
is an opaque normal-flow band with `inline-size:100%`, `box-sizing:border-box`, a `4px` graphite
block-start border, `min-block-size:44px`, and responsive padding. Its exact layout authority is a
two-column grid: `grid-template-columns:minmax(0,1fr) auto`, `align-items:end`, and a responsive
gap. `#lander-controls` is the first child, has zero margin and `min-inline-size:0`. Each of its two
`.lander-controls-line` children is `display:block` and `white-space:nowrap`; neither line may wrap,
clip, use an ellipsis, scale independently, or create an inline scrollbar. Exit is the second child
with `justify-self:end` and `align-self:end`, so it occupies the rail's bottom-right in every active
state. Retry and Exit each use a two-row inline grid or block children: the accessible label on the
first line and `.lander-key-hint` on the second at `.75em` with unit line-height. Both native
buttons retain exact minimum `44px by 44px` targets.

At `max-width:32rem`, the rail switches exactly to `grid-template-columns:minmax(0,1fr)`, places
instructions in row 1 and Exit in row 2, uses `row-gap:.4rem`, and reduces inline padding to
`.4rem`. Exit remains `justify-self:end`. The two fixed 40-character lines therefore receive the
full rail content width rather than competing with the action column. At 320 CSS pixels and the
400-percent-zoom equivalent, the `25/16` stage keeps its own geometry and the rail grows in normal
flow without horizontal page overflow. The in-stage outcome remains `min(32rem,calc(100% - 6rem))`,
leaving the fuel gauge inset clear; its status and optional Retry stack without overlap. The stage,
gauge, status, Retry, each controls line, and Exit computed rectangles must be pairwise disjoint
except for intentional parent containment.

No pseudo-element or CSS `content` supplies status text, and no second banner, alert, output,
controller-owned message, or state-specific semantic copy exists. First effective departure clears
the status and launch-ready selector together. Entering failure writes `Crashed!` once without
moving focus. The outcome wrapper never intercepts scene input outside its native Retry button.

The rail uses sky text on a dark background and padding
`clamp(0.4rem,1.5vw,0.65rem) clamp(0.6rem,2vw,1rem)`. Its prose font size is
`clamp(0.625rem,1.8vw,0.8125rem)` with `line-height:1.25`. Because the rail begins after the
complete `25/16` stage, its border box cannot overlap any SVG terrain, platform, lander, fuel,
outcome, or Retry pixel.

Computed-box witnesses at 320 CSS pixels, the 400-percent-zoom equivalent, and `60rem` require the
gauge and outcome to stay inside the stage and disjoint, every shown action to be at least
`44 by 44` CSS pixels, Retry to follow the crash banner without overlap, and
`stage.bottom<=controlsRail.borderBox.top`. No fixed block height, text truncation, page-level
horizontal scrolling, or overlay concealment is allowed. Chromium additionally requires exactly one
client rect for each controls-line text node and `scrollWidth<=clientWidth` independently for each
line, `#lander-controls`, the rail, the game root, and the document element. This tests geometry and
wrapping without asserting the authored prose.

Agent travel, battery stage, launch readiness, and crash are model-time projections, not CSS
completion events. The battery is one sharp-cornered vertical `22 by 40` scene-unit rectangle
centered below the mast. Relative to the dynamic NOC's `buildingLeft` and `roof`, its outline is
exactly `x=buildingLeft+24,y=roof+16,width=22,height=40`, with no `rx`, terminal path, nub, or
pseudo-element. Four `12 by 5` horizontal bars share `x=buildingLeft+29` and have top coordinates
`roof+46`, `roof+38`, `roof+30`, and `roof+22` from stage 1 through stage 4. This orders them
bottom-to-top toward the centered mast. Let `C=buildingLeft+35` and `A=roof-34`, the antenna-head
center. The three separate signal paths are exactly `M(C-8) (A-4) Q C (A-12) (C+8) (A-4)`,
`M(C-15) (A-5) Q C (A-20) (C+15) (A-5)`, and `M(C-23) (A-6) Q C (A-29) (C+23) (A-6)`. Each is
bilaterally symmetric about the vertical mast centerline. The checked-in static site uses
byte-equivalent substituted coordinates.

The battery outline is always visible. At exactly 200, 400, 600, and 800 ms, stages 1 through 4
permanently fill the next bottom-to-top bar with `#d94a1e`, `#ff7a00`, `#ffe09a`, and `#7de2c5`
respectively. The physical mast and antenna head are always visible in fixed graphite `#292b30`,
independent of power state. At exactly 1,000 ms, stage 5 activates only the inner radiating arch in
`#d94a1e`; at 1,200 ms, stage 6 adds the middle arch in `#ff7a00`; at 1,400 ms, stage 7 adds the
outer arch in `#7de2c5` and marks the site powered. Earlier bars and arches remain, so count and
nested symmetric shape communicate every stage without color. Once powered, attributes remain on
that retained site. Reduced motion creates no intermediate projection and applies all four bars,
three arches, powered state, and the `Agent Deployed!` banner atomically.

Pure `agentInstalled(site)` returns `site.powered || (site.nocStage ?? 0)>=1`. The controller writes
that result as `data-agent`. Set rendered SVG coordinates `B=structure.buildingLeft*10` and
`T=548-site.platformTop*10`. For `absent`, the existing `.noc-entry` path is exactly
`M B (T-18) H (B+13) V T H B Z`, has no transform, and the exact
`.lander-site[data-agent="absent"] .noc-entry` override restores
`fill:#3b3f47;stroke:#4b4e55;stroke-width:2`, preserving the exact doorway.

For `installed`, that same path, and no added child, gets this exact local compound path. The first
closed subpath converts the global agent's `rect x=-5 y=-9 width=10 height=11 rx=1` into explicit
line and radius-one arc commands; the remaining subpaths are the global glyph's unchanged terminal
and legs:

```text
M -4 -9 H 4 A 1 1 0 0 1 5 -8 V 1 A 1 1 0 0 1 4 2
H -4 A 1 1 0 0 1 -5 1 V -8 A 1 1 0 0 1 -4 -9 Z
M -3 2 V 9 M 3 2 V 9 M -2 -5 L 0 -3 L -2 -1 M 1 -1 H 3
```

Set `transform="translate(B+6.5 T-9) scale(0.75)"`. The unscaled combined bounds are exactly
`x=[-5,5],y=[-9,9]`; installed bounds are therefore `x=[B+2.75,B+10.25],y=[T-15.75,T-2.25]`, inside
the `13 by 18` opening. The installed override is the exact
`.lander-site[data-agent="installed"] .noc-entry` rule with
`fill:#2ed49b;stroke:#f5f2e8;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round`. The
filled terminal lies over the same green body and its pale centerline remains visible; leg subpaths
have zero fill area. Static site 0 is `data-agent="absent"`; runtime creation and same-window
reconciliation both write the attribute, exact `d`, and transform from the same helper, with paint
derived only from these two CSS selectors. Switching back to absent restores the exact doorway path,
removes `transform`, and selects every absent paint value above.

This projection deliberately adds no `installedAgent`, ID set, child group, use element, or world
descriptor. The existing model-owned `nocStage` and `powered` values are sufficient. Since every
checkpoint freezes powered retained sites at stage 7, restore/Retry projects the agent immediately;
later retention keeps it for every powered site still in the three-site window. Exit and reload
restore the static absent doorway with the rest of the pristine fragment. This presentation changes
no route, physics, geometry, world, or digest authority beyond section 10.2's separately pinned
Phase 4L regeneration.

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
reveal Retry; and set status exactly `Crashed!`.

The former instructional failure sentence is deleted rather than retained as hidden or duplicate
copy. Crash adds only the native Retry button beneath `Crashed!`; its smaller second line shows the
presentation-only `r` hint. Exit remains in the controls rail with its presentation-only `<esc>`
hint. The controls prose names only flight controls and omits both shortcuts. The sole status
authority therefore owns only `Crashed!` for failure and never repeats control guidance.

There is no smoke, dust cloud, atmospheric shock wave, sustained flame, sound, vibration, camera
shake, page movement, or layout change. Section 8's active angular assist applies only to a live
thrusting lander; it never changes a fragment's stored angular velocity or ballistic formula.
Reduced motion still increments the ordinal, skips flash and fragment travel, creates exactly zero
debris nodes, and enters the same final failed state in the contact task.

## 14. Deterministic vectors

Numerical physics tests use tolerance `1e-10`; selected canonical route-pose replay uses the pinned
`1e-9` tolerance from section 10.2. Machine-owned identifiers and enum strings, integers, states,
seed values, DOM order, and serialized world descriptors are exact; section 4's authored prose is
human-reviewed rather than asserted. Every schedule includes an explicit final callback.

| Vector                | Input                                                                                        | Expected result                                                                                                  |
| --------------------- | -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Gravity, 120 steps    | `(10,30,0,0)`, zero angle/engines, fuel 30                                                   | `x=10`, `y=28.4875`, `vx=0`, `vy=-3`, fuel `30`                                                                  |
| Collective, 120 steps | Same pose, engines `(0.72,0.72)`                                                             | `y=35.0215`, `vy=9.96`, angle/x unchanged, fuel `28.56`                                                          |
| Turn-only vector      | One step from same pose, raw engines `(0,0.375)`, `s=-1`                                     | `ax=-1.6875`, `ay=-0.07716426222751949`, `omega=-0.25`, angle `-0.00208333333333`, fuel `29.996875`              |
| Combined turn vector  | One step from same pose, raw engines `(0.2125,0.5875)`, `s=-1`                               | `ax=-3.6`, `ay=3.235382907247959`, `omega=-0.25`, angle `-0.00208333333333`, fuel `29.993333333333332`           |
| Angular assist        | One step, angle `0`, omega `15`, raw engines `(0.72,0.72)`                                   | engines `(0.66,0.78)`, `s=0`, omega `14.92`, angle `0.124333333333`, fuel `29.988`; total thrust unchanged       |
| Vacuum coast          | One step, angle `0`, omega `15`, zero engines                                                | omega remains `15`, angle `0.125`, `vy=-0.025`; no translational or angular damping                              |
| Exhaustion            | Fuel `0.005`, one step, engines `(1,1)`                                                      | Effective engines `(0.3,0.3)`, fuel exactly `0`                                                                  |
| Pointer vectors       | Rightward normalized drag `m=0,0.5,1`                                                        | `(.72,.72)`, `(.65375,.46625)`, `(.5875,.2125)`; leftward values mirror exactly                                  |
| Mixed input ceiling   | Keyboard collective plus pointer full right                                                  | pointer owns `s=1`; engines `(.5875,.2125)`, total `.8`, never component-combined                                |
| Keyboard steer owner  | Keyboard left plus pointer full right                                                        | keyboard owns `s=-1`; engines `(.2125,.5875)`, total `.8`                                                        |
| Canceled steer owner  | Both keyboard steers plus pointer half right                                                 | keyboard cancels; pointer owns `s=.5`; engines `(.65375,.46625)`, total `1.12`                                   |
| Empty-fuel direction  | Fuel `0`, raw engines `(.5875,.2125)`, retained physics `s=1`                                | effective engines `(0,0)` and stored/rendered `commanded.vectorAngle=0`                                          |
| Plumes                | `u=0,0.5,1`                                                                                  | scales `0.08,0.54,1`; opacities `0.25,0.625,1`                                                                   |
| First site            | Any normalized seed                                                                          | ID `0`, center `36`, span `[31.2,49.8]`; first of levels `83/91/99` clearing native max+2.4; native stays exact  |
| Deck termination      | Any current level `83/91/99`; all nine candidates in seeded order                            | an exact level-99 route exists (`+16`, `+8`, or flat respectively); at most nine checks for every seed           |
| Terrain continuity    | Retained range crossing chunks, both sites, and all twelve column-rail feet                  | one strict-X chain; boundary Ys equal; two render paths; open stroke has no floor/vertical/closing segment       |
| Structure parity      | Static and dynamic site with platform top `p`                                                | one 18.6 m path; 14 fixed truss members plus exactly three bounded variable lattice columns                      |
| Truss envelope        | Relative chords `[-4.8,13.8] x [-1.1,-.35]`, member width `.2 m`                             | collider `[-4.9,13.9] x [-1.2,-.25]`; deck/NOC overlap; top remains `.25 m` below landing face                   |
| Column envelopes      | rail pairs `0/1,8.8/9.8,17.6/18.6`; six native feet; top `p-.35`                             | each collider is the exact stroked axis-aligned box through its lower foot; all members contained and joined     |
| Aperture bounds       | Raw truss, `.8 m` lattice bays, and the native-slope wedge                                   | diameters `3.1894356867634124`, `1.2806248474865698`, and at most `1.2206555615733703`, all below hull `3.2`     |
| Connected clear face  | Every independently split face for each pinned site descriptor                               | actual maximum axis-aligned envelope equals fixture fields; `hypot(width,height)<=3.1894356867634124`            |
| Opening gauge         | Fresh run `fuel=15`, `fuelGaugeReference=30`                                                 | exact level `.5`, exact accessible reserve `15.0`; no cap or hidden extra fuel                                   |
| Later gauge           | `fuel=37.5`, `fuelGaugeReference=50`, then checkpoint restore                                | level `.75`, level `ready`; restore reproduces both values and never caps fuel                                   |
| Gauge contrast        | danger/caution/ready against `#20232a`; gauge level zero                                     | ratios `5.068/8.584/8.243`; graphite boundary plus colored inset remain visible with zero-height fill            |
| Refuel projection     | pre-award level `.25`; normal landed time `0,.15,.299,.3 s`                                  | levels `.25,.625,.9975,1`; one can follows the same linear progress and is absent after `.3`                     |
| Refuel CSS frame      | stage rect `(100,50,1000,640)`, can scene `(130,433)`, gauge rect `(120,70,16,112)`, `p=.25` | viewport can `(230,483)`, local endpoints `(130,433)` to `(28,76)`, transfer center `(104.5,343.75)`             |
| Transfer silhouette   | DPR 1, integer CSS-pixel center; computed `::after` plus paired on/off `20 by 22` crops      | six pinned layers/sizes/positions/colors; probes hit every outer/inner part and `(0,0)`/`(19,21)` match baseline |
| Reduced refuel        | Same contact with reduced motion                                                             | full model/fuel text/gauge/checkpoint atomically; `refuel=null`, no transfer pseudo-element                      |
| Launch-ready hold     | 10 seconds zero or steer-only input after power                                              | centered pose, fuel, mission time, zero command, and status remain unchanged                                     |
| Manual departure      | Launch-ready plus Space/Up, either plus vi/arrow steer, pointer/touch hold, or eligible tap  | every qualifying path uses the ordinary mixer; first step burns/integrates; `flying` starts only after `.05 m`   |
| NOC stages            | Power sequence at `0,.2,.4,.6,.8,1,1.2,1.4 s`                                                | stages `0..7`: installed agent at stage 1, four bars, then three arches; banner only at final stage              |
| Agent travel          | Deploying time `0,.225,.45,.675,.899,.9 s`; then hide document for `.3 s` at `.45`           | progress `0,.25,.5,.75,.998888...,null`; hidden interval freezes `.5`; power still begins exactly at `.9`        |
| Installed retention   | Powered sites retained through next leg, crash, and two checkpoint restores                  | each existing NOC-entry path stays installed; exact world count remains 75 and no can/power state duplicates     |
| Outcome/action rail   | Launch-ready, then failed                                                                    | banner-only deployed state; crashed status plus Retry; Exit stays bottom-right in the active rail                |
| Interactive pointer   | `pointerdown` targets Retry descendant and Exit descendant, then native click                | Retry guard has no stage flight effect; Exit cannot reach stage; each native click runs exactly once             |
| Interactive keyboard  | Focus Exit/Retry; target each button or nested span with Space, Enter, arrows, `h`, and `l`  | no flight prevention/held edge/queue/thrust; Space/Enter run one native action; arrows/`h`/`l` run no action     |
| Outside-shell keys    | Active mission; target header, breadcrumb, and descendants with Escape, `r`, and flight keys | no prevention, focus/state/action/input/model change; outside keyup is also inert after focusout clears input    |
| Controls lines        | 320 px and 400%-equivalent layouts; keyboard child then touch child                          | one client rect per line; every relevant `scrollWidth<=clientWidth`; Exit in row 2; no authored-copy assertion   |
| Refuel ratio          | Base `n=1,2,3,4`; test minimum `8`; carried fuel `7,20,5,4`                                  | ratios `2,1.5,1.25,1.125`; awards `16,12,10,9`; reserves `23,32,15,13`; next ratios direct from `n+1`            |
| Ratio precision       | Direct Number formula at bases `52,53,54,100`                                                | `1.0000000000000004`, `1.0000000000000002`, `1`, `1`; never below `1`, no bound or arbitrary precision           |
| Safe inclusive edge   | Target top; `vx=2.2,vy=-3.6,angle=-18,omega=26`                                              | safe contact                                                                                                     |
| Unsafe epsilon        | Four contacts, each increasing exactly one boundary magnitude by `1e-9`                      | each is unsafe; mirrored absolute-value signs and positive-`vy` rejection are independently covered              |
| Swept unsafe equality | Hull only grazes terrain/truss/column/NOC/mast between step endpoints                        | closed 0.02 m expansion detects it; no visual tunneling                                                          |
| Target-top separation | Safe descent over deck center; then a separate exact tangential graze                        | descent uses true top crossing and can be safe; unresolved graze is unsafe                                       |
| Frame equivalence     | Initial approach, no input, callbacks to 1,000 ms at 30, 60, and 120Hz                       | 120 steps; `x=30.8`, `y=30.0875`, `vx=0.8`, `vy=-3.4`, fuel `15`                                                 |
| First landing         | Seeds/levels `1/83,8/91,13/99`; section 7.1 off/on/off schedules; opening fuel `15`          | safe contacts at steps `554,501,512`; pre-award fuel `13.704,13.848,13.848` within `1e-12`                       |
| Free exploration      | Cross `x=-5`, `101`, target right edge, then reverse across target and both values           | stays flying absent real contact/ceiling/overspeed; camera continuous; cue right/left/right; progress unchanged  |
| Sky parallax          | Same seed, camera left `0,50,-50`; five derived sky chunks                                   | transform `0,-120,120 px`; 20 stars and 1-2 landmarks; two path nodes and exact regeneration on return           |
| Checkpoint replay     | Award, manual launch, crash, Retry twice                                                     | exact deep checkpoint projection both times; no can, award, ratio, route, power, or progress duplication         |
| Initial Retry         | Crash before first powered base, then click Retry and later use `r`                          | exact same-seed initial pose/site/window/fuel/progress/ratio; shell focus; no synthesized input                  |
| Catalog quantum       | Every checked-in reference template                                                          | allowance `minimum` matches literal safe contact; `minimum-0.05` matches literal failure                         |
| Short-tap capture     | Down at `0`, eligible up at `20`; release synchronously emits lost capture                   | token/deadline exist before release; pulse remains through `139.999`, ends once at `140`; later loss is no-op    |
| Input overflow        | 65 alternating edges before one step at 30, 60, and 120 Hz                                   | queue becomes one next-step physical-state snapshot; all frame schedules produce the same result                 |
| Long run              | 100 successful deterministic sites                                                           | ratios are non-increasing and `>=1`; O(1) direct formula; bounded nodes/edges; exact reserve accounting          |

The three first-landing last-clear poses, in level order `83/91/99`, are exactly
`(33.68666666666724,8.30415833333318,.8,-2.5610000000000563,0,0)`,
`(33.333333333333854,9.121183333333207,.8,-2.5320000000000604,0,0)`, and
`(33.4066666666672,9.912133333333205,.8,-2.807000000000054,0,0)`. The next fixed step reaches the
safe target-top contact and settles to the platform center. Tests compare the contact kind, step,
pre-award reserve, and pose with `1e-10` tolerance; none calls a production helper to build its
expected schedule.

World tests pin complete JSON descriptors and route-proof digests for seeds `11`, `39`, and `41`,
plus an independently authored static-scene vector. The fixtures begin with these exact values;
traversal is `offset,direction; motifIndex(q=0..3)`:

| Seed                | `mixUint32(seed)` | Traversal      | Chunk 0 heights                                                                                                   | Native minimum; top      | Leg-1 template preference     |
| ------------------- | ----------------- | -------------- | ----------------------------------------------------------------------------------------------------------------- | ------------------------ | ----------------------------- |
| `11`                | `69794299`        | `0,1; 0,1,2,3` | `5.255777672748081,6.955116862594149,2.3544560524402183,4.953795242286287,1.3531344321323555,1.7524736219784245`  | `6.921715945067815; 8.3` | `99,84,96,81,93,78,90,102,87` |
| `39`                | `1800816653`      | `1,3; 1,0,3,2` | `2.5975386273348704,0.5976523105753584,1.9977659938158465,5.097879677056335,3.997993360296823,3.0981070435373113` | `7.365893319045194; 8.3` | `90,102,87,99,84,96,81,93,78` |
| `41`                | `1371730420`      | `2,3; 2,1,0,3` | `1.5861064405180514,3.1087847214890645,5.331463002460078,4.05414128343109,2.1768195644021042,4.699497845373116`   | `7.049044279753696; 8.3` | `78,90,102,87,99,84,96,81,93` |
| `STATIC_WORLD_SEED` | `1076842847`      | `3,1; 3,0,1,2` | `4.29865836398676,3.1665419081225994,6.134425452258438,7.5,4.870192540530115,5.638076084665954`                   | `9.584423104863614; 9.9` | `78,90,102,87,99,84,96,81,93` |

The static row is the exact no-JavaScript site-0 descriptor for `0x41475731`; it is not an extra
seed in the 81 derived descriptors. The implementation commit also records literal template
schedules, success/failure vectors, envelopes, instantiated-site descriptors, and proof digests from
section 10's independent derivation; tests must not generate expected values by calling the function
under test. For each pinned derived seed, tests cover at least three sites, the exact motif bank and
traversal across positive and negative chunks, terrain diversity, preference and eligibility order,
guaranteed level-99 selection, six rail-foot interpolations per site, contact-time offscreen
placement, both proof replays, exact award, and rolling-window eviction.

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

Final ordinary generation and independent verification pin geometry
`a5120d97782b73afb43cabae038412252f644656f41c0ab9e33f5413da9be7ca`, physics
`e08f8260b723dd245db88de9ae2cdbac54bf9a97cb0bed1b6f170eda362c48dc`, world
`c666bb42918301f93386bb1373e92da662d333006d8684946fd80a10761d1e32`, and output
`a922372760f850386810fd6eb60f7aa807bac8b03ee5f0a2b1dec1968ee27b69`. Geometry/world and every route
record remain byte-identical; the final tolerance bump changes physics/output only.

| Layer                                                                   | Required coverage                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `node --test website/tests/lander-world.test.mjs`                       | Independently reconstruct native minima and integer deck levels; the bounded strict-X terrain projection; two open render paths; 14 fixed truss members; exactly three lattice columns with six independent native feet, bounded levels, alternating braces, exact variable member counts, exact stroked colliders, and all aperture proofs; static/dynamic parity; and complete 81-witness regeneration. Pin exact geometry/world digests and mutation-kill any floor closure, foot/member/order/style drift, collider mismatch, or aperture diameter at or above `3.2 m`. World descendants remain exactly 75 at maximum.                                                                                                                                                                                                                                                                                                                                             |
| `node --test website/tests/lander-model.test.mjs`                       | All-nine success and one-quantum exhaustion replays remain exact. Pin all four inclusive `2.2/3.6/18/26` limits and their epsilon failures, plus real ceiling/overspeed/terrain/structure collision while crossing arbitrary negative and positive X without failure. Exact vectors cover opening `fuel=15` and reference `30`, the three independently authored first-contact schedules and reserves, direct O(1) refuel ratios, uncapped carried-excess addition, checkpoint restore of fuel and reference twice, exact initial Retry to `15/30`, zero-fuel ballistic continuation, `.9 s` agent travel, unchanged `.3/1.4 s` stages, reduced-motion atomic projection, and hidden-time freeze. No assertion encodes authored prose.                                                                                                                                                                                                                                  |
| `node --test website/tests/lander-phase4l.test.mjs`                     | Mutation-sensitive controller/DOM tests pin exactly two controls-line children in keyboard/touch order, Retry label-source and hint structure without asserting text, internal `RESTART` dispatch, crash focus stability, click/`r` teardown-render-focus order, shell focus with `preventScroll`, and no synthesized input. Existing outside-shell and native-button rejection coverage remains exact.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `node --test website/tests/lander-phase4m.test.mjs`                     | Mutation-sensitive controller/DOM tests pin five sky chunks, 20 stars, one or two deterministic landmarks in exactly two paths, the complete two-arc crescent, all three one/two-ring profiles, exact circle/ellipse intersections, omitted rear-center arcs, and complete foreground arcs. They retain static/dynamic descriptor equality, bounded reconciliation, `.24` parallax transforms, negative/positive camera following, bidirectional cue changes, pass/reverse/return, and no horizontal-bound failure. They also pin the exact opening half-gauge, post-award full reference without cap, `.9 s` deploy travel, unchanged refuel/power timing, hidden-time freeze, reduced-motion atomic projection, and structural copy/link/accessibility sources without embedding authored wording.                                                                                                                                                                    |
| Derivation CLI fixture verification                                     | Generate to a temporary output with v5 deriver, unchanged v3 recipes, geometry v4, and derived v4; review the canonical delta, update all digests atomically, then run ordinary `--verify`. Generation and verification each evaluate exactly 4 combinations per template, 36 total, while 256/template and 2,304 total remain ceilings. All 162 selected replays pass; all nine route/failure literals and 81 world descriptors remain byte-identical. Geometry/world retain section 10.2's literals; physics/output change to the final tolerance literals pinned there.                                                                                                                                                                                                                                                                                                                                                                                              |
| `python -m unittest discover -s website/tests -p 'test_*.py'`           | The validator pins one shared fragment, exact sky/site/stage/outcome/Retry/rail/controls/Exit parent and source order, exactly two sky paths, exactly two controls spans, native action label/hint sources, shortcut ARIA, direct fragment-free footer Lander destinations, equal nonempty footer `aria-label`/`title`, v4 fixture/schema names, and no obsolete regional or pylon fields. It validates structure and accessible-name sources, never authored title, heading, 404, controls, status, action-label, or hover wording. Exact artifact, DOM-budget, privacy, module-DAG, route uniqueness, recovery, and static/dynamic parity contracts remain.                                                                                                                                                                                                                                                                                                           |
| Automated Chromium projection witness                                   | At 320 px, 400%-equivalent, touch landscape, and 60rem, prove the controls/rail fit, Retry derives its computed name from the visible label while excluding the hint, and crash/Retry focus behavior remains exact. Cross both `x=-5` and `x=101`, pass the target, reverse, and return; prove no horizontal failure, continuous camera projection, right/left/right cue changes, exact `.24` sky parallax, five chunks, 20 stars, one or two landmarks, stable two-path DOM, and exact reconstruction on return. Begin at `15/30` and half fill; after award prove exact uncapped reserve/reference and full fill. At exact zero prove the red border/background plus the `700ms` stepped infinite blink; reduced motion retains the static red warning. Time normal, reduced, and hidden deployment vectors, then restore the checkpoint twice and initial approach once with no duplicate service work.                                                              |
| Pseudo-can computed-style and screenshot witness                        | For `getComputedStyle(stage,"::after")`, assert `width=20px`, `height=22px`, `pointer-events=none`, `image-rendering=pixelated`, transparent background color, exactly six gradient images, sizes `6px 2px,10px 6px,2px 4px,4px 8px,12px 14px,16px 18px`, positions `6px 2px,4px 0px,16px 10px,16px 8px,2px 6px,0px 4px`, `no-repeat` six times, and alternating normalized paints `rgb(217,74,30)`/`rgb(41,43,48)` in the pinned top-to-bottom order. At DPR 1 and an integer transfer center, take exact `20 by 22` CSS-pixel crops with refueling on and off: on-crop probes `(5,1)`, `(7,3)`, `(18,9)`, `(16,11)`, `(1,5)`, and `(3,7)` prove the six graphite/orange parts; `(0,0)` and `(19,21)` are byte-equal to the off-crop background, proving transparency. The crop visibly reads as one block can. No golden asset ships.                                                                                                                                 |
| Human-authored copy review                                              | A reviewer compares the document title, headings, 404 explanation, controls, outcome/status, action labels, and visible shortcut hints with sections 4 and 13. This is deliberately human evidence; automated suites do not encode authored phrases, substrings, or blacklists.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Manual Chrome and Edge pre-merge; Firefox and Safari/WebKit post-launch | Confirm untouched coarse/random terrain stays continuous beneath each elevated site; the shallow Warren truss and exactly three open lattice columns read as one integrated load-bearing structure from platform through NOC, with six honest native-terrain feet, no backing face, seam, floating foot, or vertical terrain artifact. Travel freely past and back across the target in both directions; inspect slower stars, recognizable crescents, and planets with one or two restrained rings without pop or gameplay effect. On every ring, confirm the rear center disappears behind the planet while the foreground arc remains visible. Confirm the fixed mast/head stay graphite while only signal arches gain color, plus the exact current Lander title/heading, 404 explanation, footer hover/accessibility copy, direct `/lander/` navigation, half-full opening gauge, normal/reduced deployment timing, checkpoint Retry, and inclusive landing edges. |
| Responsive, zoom, focus, and accessibility acceptance                   | At 320 CSS pixels, 400-percent zoom, touch landscape, and `60rem`: stage and rail remain normal-flow separated; gauge/outcome, crash/Retry, controls lines, and Exit do not overlap or overflow; buttons are at least `44 by 44`. A real accessibility-tree witness derives expected names from current visible label nodes, proves hint exclusion and shortcut ARIA, one live region, controls IDREF, and tab order shell then Exit or shell then Retry then Exit. Retry returns to shell, Exit to Start; no trap. Authored strings are reviewed by a human, not embedded in automation.                                                                                                                                                                                                                                                                                                                                                                               |
| Performance and longevity witness                                       | For each seed `11`, `39`, `41`, and `STATIC_WORLD_SEED`, generate and power 100 sequential sites with no generation error, more than nine eligibility checks, terrain ordering fault, or level outside `83/91/99`; the selection vectors below remain pinned. Keep exactly two terrain paths, at most three sites, eight fragments, 80 world descendants and exactly 75 at maximum, exactly two native action descendants, at most 51 projected terrain vertices, exactly five sky chunks represented by 20 stars and one or two landmarks in one group/two paths, and no retained sky history. Each site still owns one scaffold path despite variable lattice members. Refuel ratio, camera, and sky derivation remain bounded constant work; fixed timers, frame ceiling, lifecycle teardown, and bounded world history remain exact.                                                                                                                                |
| Permanent documentation and repository gates                            | `website/README.md` and browser checklist teach the changed actions, ordinary departure, tolerances, rail, accessibility, shared-fragment/no-JS behavior, and derivation workflow in lockstep. Focused suites, deterministic root/project builds, complete gates, file lint, locked-SDD, Rulesync drift, module-size report, and an exact intended-file diff pass. Permanent docs do not link to this SDD.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |

In template order `[78,81,84,87,90,93,96,99,102]`, the 99 generated legs after the initial site have
exact selection-count vectors `[11,9,16,11,4,15,11,7,15]` for seed `11`, `[11,5,19,5,0,26,7,12,14]`
for seed `39`, `[9,6,16,8,3,28,7,10,12]` for seed `41`, and `[5,6,15,8,0,21,13,8,23]` for
`STATIC_WORLD_SEED`. Each run visits all three levels, ends at level `91`, `91`, `91`, and `99`
respectively, and proves bounded construction rather than fixture-only route replay.

Mutation tests reject duplicated/moved shared markup, a second scheduler/controller/site authority,
game checks added to the near-limit validator, artifact count drift, a sixth retained chunk, more or
fewer than the exact fill/surface terrain pair, a stroked fill, closed surface, surface floor point,
internal closure edge, concatenated per-chunk path, non-increasing X, same-X/different-Y pair, or
chunk/site boundary mismatch. They reject `10 m`/boundary/motif-bank/selector/clamp drift, a single
or repeated terrain motif, any shelf/flat site replacement/discard-resume splice, relief at a site
footprint or corridor endpoint, a column rail foot not equal to native interpolation, minimum-deck
arithmetic drift, non-integer or accumulated deck-level authority, a level outside `83/91/99`, or a
candidate selected below its native minimum.

Structure mutations reject pad-width/clearance drift, a filled scaffold face, backing rectangle,
sky-colored artifact, scaffold fill other than `none`, member width/color/segment drift, a missing
cap attribute or cap other than `butt` (including square and round caps that extend past endpoints),
a missing join attribute or join other than `round` (including miter), a rendered stroke point
outside the exact `+/-0.1 m` expansion, any truss span other than `18.6 m`, truss bay count other
than 12, bay width other than `1.55 m`, depth other than `.75 m`, or missing/extra chord or truss
diagonal. Column mutations reject a count other than three; rail pairs other than
`0/1,8.8/9.8,17.6/18.6`; a foot not independently interpolated from native terrain; a top other than
`platformBottom`; a lattice floor other than the higher foot; a bay above `.8 m`; a level not
produced by the exact ceiling/subdivision rule; a missing/extra rail, tie, or alternating diagonal;
the wrong first diagonal; a per-column member count outside `9..27`; or a whole scaffold count
outside `41..95`. They reject a regional perimeter, post grid, X brace, any surviving
platform/connector/NOC underframe collider field, a truss collider other than
`[-4.9,13.9] x [-1.2,-.25]`, any column collider other than the exact stroked axis-aligned box
through its two feet, any raw-truss/lattice-bay/terrain-wedge aperture diameter at least `3.2 m`, a
reported actual maximum connected clear face that differs from independent enumeration, a fixed
full/half-face inventory, a NOC lower bound other than `platformBottom`, a deck/NOC/truss/column
visual-collision seam, disagreement between member geometry and conservative colliders, an extra
scaffold wrapper, or a production import outside the exact DAG. They also reject fuel caps, rounded
model fuel, an opening fuel/reference other than `15/30`, retaining `legDepartureFuel`, a gauge
based on invented capacity or current award instead of `fuelGaugeReference` plus the one pinned
refuel interpolation, an accessible duplicate meter, gauge level outside `[0,1]`, can recollection,
or proof dependence on carried fuel. They reject a second banner/live region, a generated duplicate
status copy, status-wording-driven banner selection, automatic launch, gravity or fuel burn while
launch-ready, steer-only release of the pad hold, discarding the first collective step, a time-based
launch exit, transition before both feet clear, Retry into an already started launch, duplicated
can/fuel/ratio/progress/power, a missing or non-native Retry or Exit control, hidden-but-enabled
actions, wrong active-state Exit projection, or a second launch/status authority. Phase 4K
specifically rejects any `lander-launch` element, controller lookup/property/listener/handler,
fake-DOM member, validator required ID, action-order expectation, CSS selector, `launch-button`
pulse source, or detached/hidden substitute.

Phase 4J presentation mutations specifically reject fuel label/value without `.visually-hidden`,
`aria-hidden` on either, a visible numeric amount, an element other than the one ordinary
`span#lander-fuel-value`, any `output` element, role/live semantics on either fuel span,
`aria-labelledby`/`aria-describedby`/`aria-label` on either, omission or reversal of the shell's
separate label/value IDREFs, an accessibility-tree description that loses or duplicates either
current DOM-derived segment, a controller write other than `textContent=fuel.toFixed(1)`, any
semantic or named gauge, stale `data-fuel-band`, a level name or threshold outside
`empty===0<danger<=.2<caution<=.5<ready`, a level color other than the three pinned bright tokens, a
contrast ratio below `3:1` against `#20232a`, a non-graphite outer boundary for nonempty fuel, or a
missing colored inset at nonzero fill. Empty-state mutations reject a nonzero trigger, any color
other than danger red, a non-red border/background, a keyframe name other than
`agw-fuel-empty-blink`, duration other than `700ms`, non-stepped timing, opacity floor other than
`.3`, a missing infinite iteration, failure to pause with `data-paused`, or continued animation
under reduced motion. They reject a refuel duration other than 300 ms, a controller-owned refuel
timer/state, interpolation from post-award rather than pre-award level, a nonlinear or CSS-time
fill, more than one visible can, a transfer DOM/world node, viewport/local coordinate mixing,
omitted stage-rectangle offsets, contain letterboxing, a pseudo-can not centered on its coordinates,
a transfer that survives reduced motion, hidden-time progress, missing resize reprojection, or an
award/checkpoint change caused by presentation.

Pseudo-can mutations reject dimensions other than exact `20px by 22px`, nonempty generated text,
`pointer-events` other than `none`, a transform other than the pinned centering transform,
`image-rendering` other than `pixelated`, a nontransparent background color, or any border, mask,
filter, image asset, or DOM/accessibility node. Parsed computed style must contain exactly the six
gradient images in the stated paint order, with byte-equivalent colors and exact per-layer size,
position, and `no-repeat`; adding, removing, reordering, recoloring, resizing, or moving any layer
is a failure. The DPR-1 screenshot probes and `20 by 22` crop are mutation-sensitive independently
of the computed-style assertions, so a declaration that parses correctly but paints the wrong
silhouette also fails.

Markup/CSS mutations reject chrome outside `#lander-scene-shell`, controls before or over the stage,
an absent stage/outcome wrapper, Retry outside outcome or not immediately after status, controls
rail outside the final shell position, Exit outside the rail or not after controls prose, an overlay
action in launch-ready/error, a visible or tabbable wrong-state action, a target below `44 by 44`,
pointer capture on the rail or an action, missing composed-path/closest interactive rejection,
flight prevention/token/capture/enqueue from an action pointerdown, `touch-action:none` anywhere
except the active stage, non-system or non-monospace fonts, any new font request/directive/CSP
change, generated outcome text, a second status/banner, `aria-describedby` naming the rail rather
than controls prose, a hint included in the accessible name, missing shortcut ARIA, ambiguous or
changed outcome inset/transform rules, or a 320-pixel/400-percent box overlap or horizontal
overflow. They also reject other than two direct controls-line prose children, reversed
keyboard/touch order, either line wrapping to multiple client rectangles, clipping, ellipsis, an
inline scrollbar, failure to switch the rail to one column at `32rem`, Exit sharing the narrow
instruction row, or any relevant `scrollWidth>clientWidth`. These are structural and geometric
assertions, not authored-prose assertions. Installed-agent mutations reject appearance before stage
1, disappearance from a powered retained/checkpoint-restored site, a new installed-state authority,
a body path that does not reproduce the exact four radius-one arcs, changed terminal/leg subpaths,
transform/bounds/paint drift, failure to restore the absent doorway, a second NOC-entry or child,
any added world descendant, a maximum other than exact 75 or greater than 80, and any unexpected
route/physics/geometry/world/output digest change.

Phase 4M/4O projection mutations reject a horizontal world-edge failure or clamp, a camera that
cannot follow both signs of X, any camera dead zone other than section 6's exact two-sided formula,
a cue that cannot produce `right/left/right` while passing and returning, cue visibility after any
part of the target enters the viewport, or a mirrored cue with different geometry. They reject a sky
speed other than `.24` of camera motion, a sky window other than five `50 m` chunks, a count other
than four stars per chunk, a landmark cadence other than every fourth chunk, or a landmark other
than the seeded crescent/planet choice. Planet mutations reject a profile other than exact radii
`[(28,9)]`, `[(31,10)]`, or `[(28,9),(34,12)]`; a ring count outside one or two; circle-intersection
drift; a visible rear-center arc; a missing foreground arc; quadratic geometry; or the old full
ellipse. Sky mutations still reject a group/path count other than `1/2`, retained off-window sky
history, nondeterministic regeneration, or any sky semantics, collision, network, or storage state.
Battery/signal mutations reject a mast or antenna head that changes from fixed `#292b30` at any
power stage while retaining the three established arch colors and timings. They reject terrain
projection beyond the visible buffered interval, more than 51 strict-X vertices, or retained
offscreen sites expanding that projection. They also reject agent travel other than `.9 s`, any
change to `.3 s` refuel or `1.4 s` power, hidden-time advancement, or reduced motion that exposes an
intermediate stage.

Input and physics mutations reject component-wise keyboard/pointer engine merging, mixed-input
thrust above straight `1.44`, full-steer total other than `.8`, vector angle other than 30 degrees,
full-steer axial force above `6.235382907247959`, turn-only axial force at or above gravity, pointer
override of a nonzero keyboard steer, canceled keyboard steer blocking an active pointer, an idle or
exhausted nonzero `commanded.vectorAngle`, recording the capture-release association after releasing
capture, clearing its live pulse on synchronous lost capture, ignoring unrelated lost capture,
reusable-pointer-ID matching, an unguarded/duplicate pulse timeout, or accepting a new gesture
without atomically superseding the old pulse. They also reject a second pulse timer, token
namespace, or mixer path; any pulse source in overflow snapshots; any native action that synthesizes
thrust; active-shell-path validation after key classification, Escape, `r`, interactive rejection,
or keyup handling; accepting a stale/foreign shell; or an outside-shell game key that prevents
default, dispatches/focuses an action, or changes held-key/token/queue/pulse/thrust/model state.
They reject interactive-key rejection before in-shell Escape or failed-state `r`; a target-only
keyboard guard that misses a nested composed-path label/hint; or an interactive Space, Enter, arrow,
`h`, or `l` keydown that prevents default, consumes a repeat, changes flight-input state, moves
focus, or dispatches the model. They reject recording or later releasing an interactive-path
keydown, failing to clear accepted input on shell focusout, processing an outside-shell keyup after
that teardown, duplicate native Exit/Retry activation from Space or Enter, or native activation from
arrows/`h`/`l`. They also reject a button pointerdown that prevents default, captures, allocates a
token, or queues a pointer-flight edge; a native click that fires twice after that rejected down;
Exit reaching a stage handler; and ordinary keyboard/vi/pointer/touch departure that discards or
changes the first qualifying fixed step. They retain no passive damping, assist while coasting or
steering, assist that changes total thrust/fuel, reversed/cosmetic-only gimbal, stale 8.4/70
integration, or safe-contact limits other than exact `2.2/3.6/18/26`.

Retry mutations reject moving focus at crash time, any accessible name not derived from the visible
label child, inclusion or hiding of the visible `r` hint, missing `aria-keyshortcuts="r"`, an
out-of-shell `r`, separate click/key recovery paths, focus before the restored render, focus without
`preventScroll`, a retained frame accumulator, held key, queued edge, pointer capture, or pulse, and
any synthesized launch input. They reject checkpoint aliasing, restoring a post-checkpoint flight
pose, fuel, target, route, progress, ratio, retained can/power/NOC state, or site identity, as well
as any duplicate proof, award, collection, progress, or power transition. Before first success they
reject a fresh seed, a new initial descriptor, or any initial pose/fuel/progress/ratio/window value
other than section 7.3's exact same-run initial approach.

Presentation and proof mutations reject incomplete static terrain, a rendered gap across collision
geometry, weak or source-only scaffold/battery/signal parity, a battery `rx`, terminal path, nub, or
pseudo-element, fewer/more than four bars and three signal paths, an asymmetric signal path,
horizontal/reversed/mistimed stages, color-only meaning, route-pose canonicalization before
selection, quantized route poses or non-deck world/geometry values, canonical pose precision drift,
a reference schedule without `[1,90]`, iterative refuel-ratio advancement, trusting a ratio
inconsistent with `refuelRatioForBase(completedSites+1)`, runtime planning/search/fuel scan or a
third runtime proof replay, an unreachable catalog command, production-derived fixtures, a geometry
schema other than required v4, a route-derived schema other than v4, a deriver version other than
v5, a recipe version other than unchanged v3, a per-template recipe ceiling outside `[2,256]`,
early-success enumeration, more than 2,304 ordinary candidates, an actual declared/evaluated count
other than four per template and 36 total, confusing the 256/2,304 ceilings with enumerated counts,
missing selected verification replays, partial route/world regeneration, witness seeds other than
`[11,39,41]`, wrong world-witness nesting, derivation-tool imports, or
native/corridor/deck/column/81-descriptor digest drift that was not reviewed. Closed unsafe
collision, unexpanded target-top handling, transactional initialization, fixed retention, reversible
camera motion, normal crash debris, ballistic fragments, non-animated direction meaning, vacuum
presentation, lifecycle cleanup, privacy, and zero-runtime-network constraints remain
mutation-protected.

Refuel-ratio mutations independently reject a first powered-base number other than one, a formula
other than `1+0.5^(n-1)`, the former `3` start, the former `.82` recurrence or epsilon floor,
iteration over prior bases, using the post-increment ordinal for the current award, replacing rather
than adding to carried fuel, rounding/capping the award or reserve, advancing more than once,
recomputing checkpoint authority from post-crash state, arbitrary precision, an artificial
base-count bound, a runtime value below `1`, failure to round to exact `1` at `n=54`, or any effect
on the demonstrated minimum, route/failure literals, or route digests. Exact mutation vectors pin
`n=52,53,54,100` and prove the 100-site sequence is non-increasing and never below `1`; they do not
require it to remain strictly above `1`.

## 16. Traceability

| Requirement or decision                                                 | Pinned by                            |
| ----------------------------------------------------------------------- | ------------------------------------ |
| R6, D5: selected custom mark, twin plumes, and favicon                  | Sections 2 and 3                     |
| R7, AC5, AC19: hidden shared 404/Lander game and byte-equivalent DOM    | Sections 4, 11, and 15               |
| R8, AC6/AC24: near-half steering, keyboard/vi/touch, independent plumes | Sections 8, 11, 12, 14, and 15       |
| R9, AC8: no-JS, in-memory lifecycle, pause, focus, reduced motion       | Sections 4, 5.1, 8.2, 11, 12, and 15 |
| R18: exact Lander title/`h1` and 404 explanatory copy                   | Sections 4 and 15                    |
| R21, AC7: gauge, payoff, manual departure, battery/signal, legs         | Sections 4, 7, 10.3, 12, 14, and 15  |
| R22, AC22: seeded target, demonstrated minimum, ratio, carryover        | Sections 5, 7.3, 10, 14, and 15      |
| R21/AC22: elevated open scaffold with honest conservative colliders     | Sections 5.3, 9, 10.2, 14, and 15    |
| R22, AC23: offscreen target and motion-safe bidirectional cue           | Sections 6, 8.2, 12, and 15          |
| R23, AC24: vacuum crash and exact checkpoint Retry                      | Sections 7.3, 9, 13-15               |
| R24, AC25: arcade gauge/transfer, outcome/Retry, rail, installed agent  | Sections 4, 6, 7, and 11-15          |
| AC18: complete build only and exact local manifest                      | Sections 2 and 15                    |
| Phase 4G: focused modules, bounded work, docs, and browser evidence     | Sections 2, 6, 14, and 15            |
| Phase 4H: terrain, support, control, landing, and NOC tuning            | Sections 4-6, 8-10, 12, 14, and 15   |
| Phase 4I: gauge, banner, accessible departure, force, structure, copy   | Sections 4-12, 14, and 15            |
| Phase 4J: arcade chrome, refuel projection, and installed-agent payoff  | Sections 4, 6, 7, and 11-15          |
| Phase 4K: action rail, manual departure, and safe-contact envelope      | Sections 4, 6, 8-12, 14, and 15      |
| Phase 4L: Retry, refuel ratio, relaxed landing, and continuous truss    | Sections 1, 4, 5, 7, 9, 10, 14, 15   |
| Phase 4M: lattice, honest fuel, parallax sky, and free exploration      | Sections 1, 4-10, 12, and 14-15      |

Implementation treats this LLD as temporary design input. Permanent source, tests, and
`website/README.md` stand on their own and do not link back to this SDD path.
