# LLD: AGW Brand and Continuous Lunar Deployment Lander

<!-- cspell:ignore arcade Cascadia Consolas focusout IDREF IDREFs imul keyup Menlo -->
<!-- cspell:ignore pointerdown pointerup PRNG -->
<!-- cspell:ignore letterboxing parallax refuel reproject reprojection reprojects repower Segoe -->
<!-- cspell:ignore affinely autocorrelation halvings lerp Minkowski nonperiodicity overspeed -->
<!-- cspell:ignore significands subinterval superblocks unhashed unmarginated -->
<!-- cspell:ignore substep underframe unitless uint32 quantized quantization Warren -->
<!-- cspell:ignore smootherstep viewports -->

- Status: Phase 4S design feasible; implementation and operator acceptance pending
- Operator browser acceptance: pending
- Date: 2026-08-14
- FRD: `frd.md`, specifically R6-R9, R15-R25, R28-R29, AC26, and AC29-AC30
- HLA: `hla.md`, specifically D5 and D7
- Plan: `plan.md`, specifically Phase 4S
- Selected geometry: `logo-concept-10-twin-flame.svg`

## Supersession record

Phase 4P's broad-relief implementation was completed and reviewed, then rejected by the operator on
2026-08-12. Its normalized relief kernel, fixed global 11.6 m platform datum, variable tall support
columns, vertical camera projection, collision optimization, responsive-gauge-only workaround, v5
geometry and derived fixtures, and Phase 4P-specific tests are superseded. Commit
`56650d774e4ba3769ca293072632e5741d493396` is restored as the current Lander behavior while a new,
separately designed terrain-relief change is considered. The completed Phase 4P record in the
lead-owned plan remains historical evidence, not a claim about current production behavior.

Phase 4Q restored the fixed-height scene and local structures, but its smooth site-coupled terrain
was rejected by the operator on 2026-08-13. Phase 4R supersedes only Phase 4Q terrain generation,
terrain-derived deck/support geometry, and the route-proof data that necessarily consumes that
terrain. Controls, thrust, gravity, fixed-step integration, collision, landing tolerances, fuel
flow, refuel ratio, mission copy, sky, power-up behavior, and every non-terrain presentation remain
unchanged.

Phase 4S preserves Phase 4R's accepted fixed scene, terrain band, straight-segment presentation,
terrain-independent site centers, local deck rule, native support feet, mission pacing, and honest
sufficient allowance. It supersedes only Phase 4R's forced 128 m high/low alternation, its
associated terrain/route fixture bytes, the synthetic vertical-ceiling and overspeed crash paths,
and collision traversal that was bounded by retained render geometry. No control, thrust, gravity,
integration, landing limit, fuel consumption, refuel ratio, site spacing or ordinary site structure,
sky, mission copy, power-up timing, or page-layout contract changes. Phase 4S chooses R29's
permitted finite-world fallback because JavaScript Number cannot honestly enumerate terrain indexes
or rotation phases over every finite IEEE coordinate. The generated world is the closed horizontal
interval `[-393216,393216] m`; each edge is an explicitly rendered, collision-backed physical
terminus, never a retained-window boundary or an invisible failure. Sections 5.4, 7.2-7.3, and 9 pin
the narrowly necessary final-site and collision semantics.

## 1. Scope and terms

This LLD preserves the selected brand and defines the continuous Lander and arcade presentation in
R7-R9 and R21-R25, including the Phase 4M free-exploration, lattice-column, parallax-sky,
half-reference opening, faster-deployment refinement, and Phase 4S straight-polyline terrain. It
excludes main-page, onboarding, deployment, and DNS design. Use plain HTML, CSS, SVG, and
JavaScript.

A **run** begins at START and ends at Exit or reload. A run contains successive **legs**, each from
one checkpoint or the initial approach to one target site. A **site** is one platform, gas can, and
NOC. **Commanded thrust** is the post-input, post-assist, post-fuel engine value shared by physics
and plumes. **Manual steer** is the normalized signed turn intent before angular assistance;
negative is left and positive is right. **Thrust-vector angle** is the manual-steer-derived
direction shared by both engine forces and both rendered plumes while effective thrust is nonzero;
it is zero at zero effective thrust. **Mission time** excludes hidden time. A **sufficient
allowance** is the section 10.3 corpus-wide controller base plus the monotone positive deck-delta
surcharge, rounded up to the pinned fuel quantum. It is neither a schedule-specific minimum nor a
global mathematical optimum over all possible controls. The **refuel ratio** for the one-indexed
successfully powered base number `n` is exactly `ratio(n)=1+0.5^(n-1)`. The first successfully
landed and powered base is `n=1`. That mathematical sequence approaches `1` from above; section 10.3
pins its exact JavaScript Number projection, including the finite-precision value `1` from `n=54`
onward. The **fuel-gauge reference** is the positive model-owned denominator used only by the visual
gauge. It starts at `30` against the opening reserve of `15`, then becomes the exact uncapped
post-award reserve whenever an award establishes a later leg. It is neither a tank capacity nor
necessarily the amount present when an initial leg starts. A **refuel projection** is the
model-owned, 300 ms presentation record that starts at the pre-award gauge level while the
already-committed fuel award remains authoritative. An **installed agent** is the visual projection
of a retained site's existing `nocStage`/`powered` state into its existing NOC-entry path; it is not
a second site-state field or world node. **Launch-ready** means the centered powered-pad checkpoint
is holding the lander at rest before the player's first effective collective command. It is
represented by `state="launching"` with `launchStarted=false`, not by another mission state or a
controller-owned flag. A **world terminus** is one of the two visible physical rails at the closed
generated-world X bounds. It is collision geometry, not a render-window edge, coordinate guard, or
mission error. The **final site** is ordinary site `4095`, the last forward target before the right
terminus; its successful service completes without inventing site `4096`.

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
| `website/build.py`                                     | Rendering, validation, and exact proof-source composition       |
| `website/site_game_validation.py`                      | Focused game DOM, module-closure, and manifest validation       |
| `website/static/lander.css`                            | Scene layout, state selectors, focus, and SVG presentation      |
| `website/static/lander-world.js`                       | Pure seed, terrain, site, collision, and window functions       |
| `website/static/lander-model.js`                       | Authored flight/run state, physics, and proof replay            |
| `website/static/lander-route-proofs.generated.js`      | Source-only generated 243-record proof projection               |
| `website/static/lander-game.js`                        | DOM, clock/input, camera, focus, lifecycle, and rendering       |
| `website/static/onboarding-copy.js`                    | Existing canonical onboarding copy; preserved without changes   |
| `website/tools/derive_lander_routes.mjs`               | Independent, deterministic route-fixture derivation CLI         |
| `website/tools/project_lander_route_proofs.mjs`        | Deterministic derived-fixture-to-source projection CLI          |
| `website/tools/lander_clear_faces.mjs`                 | Independent scaffold-overlay and clear-face enumeration         |
| `website/tests/fixtures/lander-route-derived-v7.json`  | Immutable reviewed corridor-schedule bootstrap input            |
| `website/tests/fixtures/lander-route-geometry-v8.json` | Canonical global terrain, site, structure, and envelope input   |
| `website/tests/fixtures/lander-route-derived-v8.json`  | Reviewed 243-pair schedules, openings, and witness output       |
| `website/tests/lander-world.test.mjs`                  | Seeded world, window, site, and proof-key vectors               |
| `website/tests/lander-model.test.mjs`                  | Scheduler, physics, mission, fuel, and checkpoint vectors       |
| `website/tests/lander-phase4s.test.mjs`                | Finite-world, continuous-contact, and terminal-site vectors     |
| `website/tests/lander-route-proofs.test.mjs`           | Generated projection parity and exhaustive proof replay         |
| `website/tests/test_lander_404.py`                     | Build, DOM, no-JS, and forbidden-surface checks                 |
| `website/tests/lander-browser-checklist.md`            | Package-free browser, performance, and accessibility acceptance |

The shipped artifact grows by exactly one file, `static/lander-world.js`. Tool, test, and
`lander-route-proofs.generated.js` source files are not separate artifact entries. Before ordinary
asset validation, `build.py` requires the model's one exact generated-proof import and the
projection's provenance marker, removes that source-only import, and prepends the reviewed generated
module bytes to the existing `static/lander-model.js` destination. The output therefore remains the
exact 13-file artifact and has no unresolved generated-module import. The shipped production module
DAG is exact:

```text
lander-game.js  -> lander-model.js -> lander-world.js
       |--------------------------------^  read-only projection and seed helpers
```

`lander-game.js` imports the model API plus only the pure `cameraLeftForPose`, `CHUNK_WIDTH`,
`mixUint32`, `siteScaffoldPath`, `siteStructure`, `skyProjectionForCamera`,
`skyProjectionIdentityForCamera`, `targetDirectionForViewport`, `terrainFillPath`,
`terrainSurfacePath`, and `terrainVerticesForRange` exports directly from `lander-world.js`. The
authored `lander-model.js` source additionally imports the source-only generated proof catalog;
build-time composition resolves that edge inside the existing model artifact. `lander-model.js`
imports pure world construction, retention, seed, collision, and geometry exports. `lander-world.js`
imports neither production module, reads no DOM, clock, storage, or ambient randomness, and owns no
mutable singleton. No production module imports upward or sideways outside this DAG.

The model is the sole mutable run authority. One run aggregate owns physics, fuel, mission state,
seed, generator cursor, retained sites, active and target IDs, route proof, checkpoint, and crash
debris. The controller is the sole browser adapter and owns browser listeners, the animation frame,
focus, pointer capture, CSS projection, and entropy acquisition. Neither lower module accesses a
browser global. The controller must not keep a second site, fuel, checkpoint, or mission copy.

Prefer each focused production, tool, or test module at or below 500 lines; every authored source
must remain below 1,000. Generated fixture and source-projection bytes are not authored source, but
their derivation, schema, provenance, composition, and review remain mandatory; authored logic may
not be minified or formatting-ignored to evade the ceiling. `website/site_validation.py` is already
near the hard ceiling. It imports `validate_game_contract` from `site_game_validation.py` and passes
the rendered pages, asset manifest, and site base into that focused authority. The helper imports
only the standard library, never imports `site_validation.py`, and owns all new game-subtree,
game-module closure, and exact game-manifest rules. `test_lander_404.py` exercises the helper. The
helper is build source, not a shipped site file, so the browser artifact remains exactly 13 files.
Splitting follows authority, not line compression.

Phase 4S adds no production module or shipped artifact. Replace the existing collision block inside
`lander-world.js` with section 9's candidate traversal and linear/quadratic reconstruction; do not
append a second classifier. The expected authored result is 800-900 lines for `lander-world.js`,
820-860 for `lander-model.js`, and below 720 for `lander-game.js`. Keep the existing 967-line model
test below 1,000 by placing new collision/terminus/terminal-service coverage in the focused
`lander-phase4s.test.mjs`; do not move unrelated assertions merely to game the ceiling. The ordinary
broad-phase-clear step remains O(1), an ordinary near-ground step reaches approximately 10-12
candidate halvings and one or two angle slabs, and only pathological maximum-reachable rotation can
reach the declared 50,828-knot bound. That bound may cost work but cannot become a failure.

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
            path.terrain-fill
            path.terrain-surface
            path.world-termini
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
divides by `2 ** 32`. Stream `5` remains debris and streams `6..12` remain sky. Phase 4S retires
terrain streams `13..14`; stream `15` selects one run-wide profile offset and stream `16` shuffles
each signed terrain epoch. Streams `1..4` and `13..14` remain unused. Indexed samples make
regeneration independent of call order. Neither terrain stream consumes a site index. There is no
mutable PRNG cursor inside the world module.

The checked-in preflight scene uses `STATIC_WORLD_SEED=0x41475731`. Production START requests one
`Uint32Array(2)` from `crypto.getRandomValues` and passes both words through
`mixUint32(word0 ^ rotateLeft(word1,13))`. If Web Crypto is unavailable, the controller mixes
integer `Date.now()` and the integer microsecond portion of `performance.now()` once. It never uses
`Math.random`. The resulting nonzero seed replaces the static scene and exists only in the run
aggregate. Tests call `createRun({seed})`; ordinary START, Exit followed by START, and reload
acquire a fresh seed. Retry reuses the current run seed and checkpoint.

### 5.2 Phase 4S global straight-polyline terrain

`CHUNK_WIDTH=50 m` remains retention bookkeeping; chunks do not generate terrain. Terrain is one
global lattice derived only from the normalized run seed and a signed global block/vertex index. It
does not read a site index, site center, footprint, deck, route proof, retained history, or
collision result. Sites likewise do not influence terrain phase or select among terrain profiles.

Canonical vertices occur every `16 m`. Groups of 32 segments form signed `512 m` superblocks. Each
superblock selects one of the following eight asymmetric 33-sample profiles. A profile returns to
`.35` only at its two 512 m boundaries; none of its internal 128 m cuts is `.35`:

```text
S0 .35 .40 .45 .51 .55 .58 .59 .60 .60 .60 .59 .56 .54 .54 .55 .56 .57 .58 .59 .59 .59 .57 .52 .45 .38 .37 .37 .35 .37 .29 .31 .35 .35
S1 .35 .33 .29 .24 .18 .11 .10 .10 .12 .14 .15 .15 .15 .14 .13 .11 .10 .10 .10 .13 .17 .21 .25 .28 .31 .27 .26 .23 .26 .29 .36 .39 .35
S2 .35 .30 .24 .17 .11 .11 .12 .12 .12 .14 .16 .16 .15 .14 .13 .11 .10 .10 .12 .15 .17 .21 .23 .23 .21 .27 .35 .38 .40 .32 .28 .31 .35
S3 .35 .38 .40 .41 .42 .42 .41 .40 .36 .29 .21 .14 .10 .10 .11 .12 .13 .13 .12 .11 .11 .12 .15 .21 .28 .27 .24 .27 .28 .28 .24 .30 .35
S4 .35 .39 .44 .50 .56 .58 .60 .60 .59 .57 .56 .56 .57 .57 .55 .53 .52 .51 .50 .48 .47 .47 .48 .48 .46 .40 .41 .35 .31 .23 .24 .30 .35
S5 .35 .30 .25 .20 .14 .10 .10 .10 .11 .11 .14 .18 .22 .26 .30 .32 .32 .31 .29 .29 .30 .33 .37 .40 .42 .45 .38 .36 .33 .34 .40 .39 .35
S6 .35 .35 .37 .41 .47 .51 .56 .60 .60 .60 .59 .56 .53 .51 .50 .50 .49 .48 .48 .48 .49 .49 .49 .46 .42 .35 .28 .24 .30 .33 .32 .38 .35
S7 .35 .30 .24 .20 .14 .11 .10 .10 .11 .12 .13 .14 .14 .13 .12 .11 .10 .10 .11 .13 .16 .21 .26 .31 .37 .35 .43 .45 .50 .48 .40 .40 .35
```

Selection is global and call-order independent. Set `profileOffset=floor(8*sampleUnit(seed,15,0))`.
For signed superblock `b`, set `epoch=floor(b/8)` and `slot=positiveModulo(b,8)`. Construct the
epoch's eight-profile permutation exactly:

1. `first=positiveModulo(profileOffset+epoch,8)` and `last=positiveModulo(first+2,8)`.
2. `middle` is the remaining profile IDs in ascending numeric order.
3. For `j=5..1`, set `sampleIndex=(Math.imul(epoch,6)+(5-j))>>>0`,
   `k=floor((j+1)*sampleUnit(seed,16,sampleIndex))`, and exchange `middle[j]` with `middle[k]`.
4. The epoch permutation is `[first,...middle,last]`; superblock `b` selects its `slot` entry.

This consumes only the normalized seed and signed global superblock/epoch. Every epoch uses each
profile exactly once, no neighboring superblocks repeat a profile including across epoch boundaries,
and the order admits adjacent upper-like/upper-like and lower-like/lower-like profiles instead of
forcing alternation. It does not inspect a site, route, terrain height, retained history, or
previous generated result.

For `b=floor(x/512)`, `u=x-512*b`, `k=floor(u/16)`, and `t=(u-16*k)/16`, let the selected profile
samples be `n_k,n_(k+1)`. The only terrain interpolation is `N(x)=n_k+(n_(k+1)-n_k)*t`. Project it
exactly as `worldY=64*N-9.2` and `sceneY=548-10*worldY=640-640*N`. Thus normalized zero is the top
of the instruction rail at `sceneY=640`, normalized one is the top of the fixed scene, and the
complete surface lies inclusively in `N=[.10,.60]`, `worldY=[-2.8,29.2]`, `sceneY=[576,256]`. The
seed corpus must exercise both portions of the band in ordinary windows; a merely mathematical
distant extreme is insufficient.

Every displayed and colliding edge is the straight line segment between adjacent canonical vertices.
Strictly increasing X is mandatory. No smootherstep, spline, Bézier, quadratic/cubic SVG command,
arc, rounded terrain join, CSS filter, sampled analytic curve, or presentation-only relief authority
exists. The limits remain `16 m` cadence, maximum absolute grade `.36 m/m`, maximum grade angle
`atan(.36)=19.798876354524932 degrees`, and maximum adjacent segment-grade change `.40 m/m`.
Exhaustive enumeration of the eight rows and all 64 eligible profile boundaries proves actual
maximum absolute grade `.32` and exact maximum adjacent-grade change `.40`; the looser `.36` grade
contract remains the mutation boundary. Each profile has 6-8 direction reversals with nonzero-run
spacing from 16 through 160 m, several intermediate levels, asymmetric facets, and broad climbs or
falls interrupted by smaller changes. Two-level triangles, sawtooth repetition, site-shaped low
corridors, and any return to a common height every 128 m are forbidden.

Variety is structural rather than a screenshot impression. For each seed in
`[11,39,41,STATIC_WORLD_SEED]`, independently reconstruct 4,096 consecutive canonical vertices
centered on zero and assert: all eight profiles occur; no two adjacent superblocks select the same
profile; no exact vertex period from 1 through 64 exists; each 512 m profile's internal 128 m cuts
differ from `.35`; and both `.10` and `.60` occur in the complete corpus. Record normalized Pearson
autocorrelation at vertex lags `[1,8,16,24,32,40,48,56,64]`. Lag 1 may be high because adjacent
facets are navigable, but for the reviewed seed corpus the absolute value at every lag 16 through 64
must remain below `.22`. Mutating the epoch rotation, shuffle index, first/last separation, any
profile sample, or boundary rule must fail this independent reconstruction. These are numeric world
properties, not authored-prose or golden-image assertions.

For the variety witness only, classify `S0,S4,S6` as upper-like and `S1,S2,S3,S5,S7` as lower-like;
this label does not affect selection or geometry. In signed superblocks `[-64,63]`, each review seed
must contain at least one upper-like/upper-like and one lower-like/lower-like adjacency. The frozen
corpus counts are respectively `10/42`, `13/45`, `10/42`, and `14/46` in seed order
`11,39,41,STATIC_WORLD_SEED`. Treat each ordered two- or three-superblock profile-ID tuple as an
exact silhouette signature. Across the same 128-superblock windows, the four seeds contain
respectively `50/104`, `52/113`, `53/112`, and `48/108` distinct two/three-block signatures out of
at most `127/126` positions; require minima `48/104`. A forced family alternation or short repeated
silhouette therefore fails numerically.

`terrainHeightAt(seed,x)` performs one indexed superblock/profile lookup and this linear
interpolation. Rendering, collision, closed-footprint deck derivation, each support foot, static
recovery, fixtures, and independent proof tooling consume that function or byte-equivalent canonical
vertices. A retained range is the sorted strict-X union of its clipped 16 m lattice, range
endpoints, and exact structure/column X insertions evaluated from the same interpolation. Eviction
and regeneration in either signed direction reproduce canonical bytes without mutable terrain
history.

### 5.3 Terrain-independent sites and exact local decks

Site 0 has fixed center `C_0=36 m`; every signed site uses the route-only formula `C_i=36+96*i`.
Site construction fixes this X before any terrain-height call. The `96 m` cadence is near the
accepted mission pacing and is not phase locked to the `512 m` terrain superblock: its exact spatial
cycle is 16 sites because `gcd(96,512)=32`, with center phases
`{4,36,68,100,132,164,196,228,260,292,324,356,388,420,452,484}` modulo 512. Every seed-selected
profile remains eligible; runtime does not reject, reorder, or retry a profile because of route
clearance.

First fix `platformLeft=C_i-4.8`, `platformRight=C_i+4.8`, `buildingLeft=platformRight+2`, and
`buildingRight=C_i+13.8`. Then, and only then, derive:

```text
closedFootprint = [C_i-4.8,C_i+13.8]
localNativeMaximum = max(terrainHeightAt(seed,x), x in closedFootprint)
platformTop = localNativeMaximum + 2.5
platformBottom = platformTop - .35
```

Because the native surface is piecewise linear, the maximum is evaluated exactly at the two closed
endpoints and every canonical 16 m vertex inside the footprint; no numerical scan or sample spacing
is authoritative. Do not round the deck, consult the next route, flatten native terrain, or share a
global datum. The independently enumerated finite authority spans deck tops `[-.268,31.7] m` and
adjacent deck deltas `[-19.744,17.704] m`. Native terrain remains unchanged beneath platform, truss,
columns, and NOC; there is no shelf, cap, blend, corridor relief, foundation, or site-conditioned
terrain branch.

The 16 spatial phases and finite profile closure produce 320 exhaustive geometry assignments. A leg
lies wholly within one superblock for 12 phases, yielding eight profile assignments each. Phases
`4,420,452,484` cross one superblock boundary, yielding all 56 ordered distinct profile pairs each;
equal neighbors are unreachable by section 5.2. This conservative closure includes every concrete
seeded leg geometry even when a selector relation makes an assignment rare. Exact closed-footprint
evaluation collapses these only by identical ordered integer-millimeter deck endpoints to 243 proof
keys and 224 signed deck deltas. Per-phase key counts in ascending phase order are
`[17,8,8,8,8,8,8,8,8,8,8,8,8,35,50,45]`. Equality means exact deck/structure constraints, not a
similar height or rounded floating delta. Section 10 proves all 243 without removing a profile.

Each support rail starts at `platformBottom` and ends independently at
`terrainHeightAt(seed,railX)`. Bay count remains the exact existing `.8 m` subdivision and has no
maximum or terrain-band-specific cap: supports extend however long the local deck-to-native-terrain
distance requires. Retention bounds sites and DOM nodes, not the number of members in an individual
required support. A generation invariant error enters the existing `generation-error` state; it
never causes a lower terrain profile, a new seed, a different site X, a relaxed collider, or a
fallback route.

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
span midpoint at `9.3 m`. All six rails remain inside the closed site footprint used by the `2.5 m`
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
terrain-slope wedge. The `2.5 m` deck clearance and `.35 m` platform thickness prove
`columnTop-latticeFloor>=2.15 m`, so every column has at least three bays. There are exactly
`3+2*bayCount` column members. The finite terrain band and local deck derivation make each generated
site finite, but no artificial maximum bay count or support length may truncate a legitimate column.
The complete path contains the fixed 14 truss members plus the three exact finite column sets. It
still creates no child node, backing face, regional brace field, crossing X pair, or common
foundation.

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
whose diameter is `sqrt(1.64)=1.2806248474865698 m`. The two rails of each column are exactly `1 m`
apart, so section 5.2 gives `abs(footA-footD)<=.36*1=.36 m`. The terrain wedge therefore fits inside
`1 by .36 m`, with exact diagonal `sqrt(1+.36^2)=sqrt(1.1296)=1.0628264204469138 m`. Added rails,
ties, and braces can only subdivide these bounds, and member stroke can only shrink them. The
conservative truss and column colliders are therefore honest: no complete `3.2 m` hull can enter an
opening they reject while the narrower lattice remains visibly open.

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

### 5.4 Finite generated world and visible physical termini

Set `WORLD_MIN_X=-393216 m`, `WORLD_MAX_X=393216 m`, `MIN_SITE_INDEX=-4095`, `MAX_SITE_INDEX=4095`,
and `TERMINUS_WIDTH=.2 m`. Both world bounds are exact multiples of the 16 m terrain cadence and 96
m route spacing. Canonical terrain contains every lattice vertex and linear segment in the closed
interval `[WORLD_MIN_X,WORLD_MAX_X]` and none beyond it. The pure site helper accepts the complete
signed index interval so signed reconstruction remains meaningful; ordinary mission progression
begins at site `0` and advances through site `4095`. The final site's rightmost structure point is
`393169.8 m`, leaving `46.2 m` of visible native terrain before the right terminus. No profile,
site, deck, route, seed, or retained state chooses either bound.

Each terminus is a physical vertical rail with its inner collision face exactly at the matching
world bound. The left rail occupies `[WORLD_MIN_X-.2,WORLD_MIN_X]`; the right occupies
`[WORLD_MAX_X,WORLD_MAX_X+.2]`. Its canonical collider is that closed horizontal interval times the
upward ray beginning at the canonical native terrain height at the bound. The one permanent
`.world-termini` SVG path renders both rails with the existing `#4b4e55` structure stroke,
`stroke-width="2"`, and butt caps from the fixed stage top to their native feet; clipping the ray at
the fixed stage top visually communicates continuation and is not its collider endpoint. The path is
translated by the same X-only world transform and is present in static and runtime markup. It is not
regenerated with chunks, and a retained terrain, site, or camera edge never acquires its class or
collider.

This domain is also the numeric-totality authority. At most 4,096 successful services can occur. The
exact ratio sum is `4096+2*(1-2^-4096)<4098`; the maximum ordinary allowance is `18.6`, so opening
fuel plus every possible award is strictly less than `15+18.6*4098=76237.8`. Use the conservative
integral bound `F=76238`. Existing thrust and fuel arithmetic then gives total translational thrust
impulse at most `9F=686142 m/s`, total angular impulse at most `80F=6099040 degrees/s`, and one-step
unwrapped angular travel at most `6099040/120=50825.333333333336 degrees`. A deliberately loose
positive-Y bound is `40.5F^2=235395422082 m < 2^38 m`; terrain contact plus the corresponding
ballistic fall bound keeps the first contact-crossing endpoint inside the same magnitude. The
terminus inner faces and one-step velocity bound keep every collision endpoint within `399000 m`
absolute X. These are derived reachable-state proofs, not new clamps, crashes, integration branches,
or player-facing limits. They make every terrain index, rotation-knot count, rational polynomial
coefficient, and dyadic isolating interval in section 9 finite and safely representable. A mutation
that treats a non-finite value, coordinate guard, work budget, or violated derived bound as contact
is forbidden; such a state indicates an implementation defect and takes the existing transactional
generation error only before play, never a flight transition.

## 6. Projection, camera, and bounded retention

The SVG uses `viewBox="0 0 1000 640"`, `preserveAspectRatio="xMidYMid meet"`, and no intrinsic
minimum width. `#lander-scene-stage` is `position:relative`, has `aspect-ratio:25/16`, and owns that
SVG plus every overlay. `#lander-scene-shell` is a normal-flow column, `width:min(100%,60rem)`, and
contains the stage followed by the controls rail. It has no aspect ratio of its own. This keeps the
scene's exact projection while making the rail a real terrain-separated band rather than an overlay.
Neither box can cause page overflow at 320 CSS pixels or 400 percent zoom. Horizontal scale is
`10 scene units/m`; vertical projection is always `sceneY=548-worldY*10`.

Phase 4S retains the fixed-height invariant. The stage remains a `25/16` box for the entire run; the
world transform's Y component is always zero; and no pose, terrain, site, altitude, or gameplay
state may change its aspect ratio, viewBox, projected block size, shell block size, page block size,
or a vertical camera value. Viewport size, zoom, and the pinned width media query may uniformly
scale the stage down while preserving exactly `25/16` so the complete fixed scene fits the bounded
layout; they never change scene coordinates, viewBox dimensions, or camera projection. CSS keeps
stage overflow clipped and the controls rail in normal flow. The only camera state and custom
property are horizontal. Flight beyond the fixed view may clip during free exploration, but it can
never grow or scroll the page to follow the lander. Both rendered game pages use a `100svh` bounded
grid whose header, game main, and footer fit inside the viewport; the game main has
`min-block-size:0`, is a size-query container, and owns `--controls-rail-block-size:48px`, changed
to `84px` at `max-width:32rem`. The shell's rows are `auto var(--controls-rail-block-size)` and
stage inline size is exactly `min(100%,calc((100cqh - var(--controls-rail-block-size))*25/16))`.
Only the stage shrinks on a short viewport; the normal-flow rail retains the shell width and every
action remains at least `44 by 44` CSS pixels. The middle track therefore preserves `25/16` without
block growth. At all required layouts, `document.scrollHeight==document.clientHeight`,
`scrollTop==0`, and wheel, touch, focus, thrust, camera, crash, service, Retry, and Exit cause no
vertical scroll. This is a layout invariant, not wheel/touch event cancellation; every action and
landmark remains visible and reachable without clipping.

The controller computes one reversible dead-zone camera directly from the current immutable pose and
clamps only at the two physical world rails:

```text
rawCameraLeft = pose.x < 5 ? pose.x-5 : pose.x > 35 ? pose.x-35 : 0
cameraLeftForPose(pose) = clamp(rawCameraLeft,
    WORLD_MIN_X-TERMINUS_WIDTH,
    WORLD_MAX_X-100+TERMINUS_WIDTH)
```

It writes `--camera-x=-cameraLeft*10px` on the game root. CSS applies one transform on
`#lander-world`:

```text
transform: translate(var(--camera-x), 0)
```

All terrain, sites, lander, agent, and debris retain absolute world-derived scene coordinates inside
that group. The camera holds the opening viewport while the reference point remains in `[5,35] m`,
keeps it at scene `x=50` during leftward travel and `x=350` during rightward travel, and is
continuous at both dead-zone boundaries. It has no monotonic furthest-X value or controller cache;
the only horizontal extent is section 5.4's physical world. Contact, service, crash, checkpoint
restoration, passing a target, and returning from either direction always use the current frozen or
restored pose. Each rail enters the fixed viewport before an ordinary approach can touch its inner
face; clamping may expose its `.2 m` outer stroke but never void beyond it.

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

Terrain range projection intersects its requested range with section 5.4's closed world and never
drops a segment merely because neither endpoint lies on a retained `50 m` chunk boundary. Chunk
indexes choose one closed retained range from the minimum retained chunk edge through the maximum
retained chunk edge. A pure helper emits the exact terrain height interpolated from section 5.2's
collision vertex chain at both range endpoints plus every interior vertex, in strict X order. The
result has one Y for each X, including exact chunk/site boundaries; static and dynamic projections
use the same helper output and never concatenate per-chunk paths.

Two SVG paths consume that one vertex array. `.terrain-fill` is the surface chain followed by the
two outer retained-range edges down to scene `y=648` and the floor edge, closed with `Z`; it has
`fill=#d7d2c4` and `stroke=none`. `.terrain-surface` is only the open surface chain, has
`fill=none`, `stroke=#4b4e55`, `stroke-width=2`, `stroke-linejoin=miter`, and `stroke-miterlimit=2`,
and contains no `Z` or point at `y=648`. Therefore no fill closure can be painted, and the only
stroked terrain path has strictly increasing world X with no vertical segment. Mutation witnesses
reject any same-X pair, any different Y for a shared chunk/site boundary, a surface `V`, `Z`, or
`L ... 648`, a fill stroke, or more than this one fill/surface pair. This directly prevents the
former visible vertical lines at internal retained chunk boundaries, including during forward and
backward reconciliation.

The visible interval is `[cameraLeft,cameraLeft+100]` intersected with the closed world. Retain
chunks intersecting the interval plus `40 m` on each side, at most five `50 m` chunks. Retain the
active checkpoint site, target site, and at most one immediately preceding powered site, at most
three immutable site-state records. Terrain generation uses only the retained chunk edges as its
closed range. It receives the small site records only to add exact structure and column boundaries
where that range intersects them; terrain heights remain seed/global-index values and it must never
widen the range to reach an offscreen site. The result stays below the independently generated v8
render-vertex ceiling and reconstructs identical native values from seed and signed global vertex
indexes after eviction, reversal, and return anywhere inside the physical rails. Collision
separately queries global procedural segments across the complete in-domain sweep as section 9
requires. When a terrain or sky key changes, reconcile the fixed nodes once; ordinary frames update
transforms and attributes only. The run retains no discarded terrain or site history beyond
`completedSites` and the latest checkpoint snapshot.

Hard runtime ceilings are two terrain paths, one permanent terminus path, three site groups, eight
debris fragments, 80 descendants under `#lander-world`, one sky group with exactly two path
descendants, the reviewed v8 terrain-vertex ceiling, 64 queued input records, one pointer, one
animation frame, and one pulse timer. The lattice replaces members inside each existing
`.site-scaffold` path and adds no descendant. The simultaneous five-chunk-retention, three-site,
eight-fragment projection therefore becomes exactly 76 world descendants with four nodes of
hard-budget margin. The exact count is
`4 terrain-layer nodes + (1 site-layer + 3*18 site nodes) + (1 debris-layer + 8 fragments) +`
`(1 mission-lander + 4 children) + (1 mission-agent + 2 children) = 4+55+9+5+3 = 76`; Phase 4R's
same maximum without the sole terminus path was 75. An installed agent reuses its site's existing
`.noc-entry` path, and the traveling agent keeps the existing global `#mission-agent`. The fixed
sky, scene-stage/outcome wrappers, and two native action descendants are outside `#lander-world` and
create no per-site or per-frame node. When enqueueing would create record 65, discard all queued
edges, sample the controller's complete keyboard, pointer, and sole collective-pulse state, and
enqueue exactly one `INPUT_SNAPSHOT` for the next integer simulation-step boundary. The snapshot
contains held physical codes; pointer-active, pointer ID, pointer token, and anchor/current X; plus
collective-pulse active, monotonically assigned token, and deadline. Intermediate edges are
deliberately lost; subsequent edges append after that record. This is deterministic degradation, not
an ordering-preservation claim. A 100-site browser witness must keep these counts constant, show no
increasing event-listener count, and keep active-game frame work below 4 ms at the 95th percentile
on the pre-merge Chromium machine. Direct cycle lookup, local terrain construction, and exactly two
proof replays together must finish below 25 ms at the 95th percentile and 50 ms maximum over the
same witness; record actual results rather than weakening the ceiling.

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
descriptors returned by `lander-world.js` are immutable values. Fuel is an uncapped nonnegative
JavaScript number measured in engine-seconds; section 5.4 separately derives the finite maximum a
complete run can ever receive. There is no tank capacity, runtime clamp, or award replacement:
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

The opening reserve has the eight independent, fixed-step S0-S7 feasibility witnesses in section
10.2. Each starts from this exact pose and fuel `15`, commands zero engines, then straight
collective `(.72,.72)`, then zero engines until first contact. The schedules are test-only
witnesses, not a runtime assist, planner, award input, or claim of minimum fuel. Together they cover
every eligible first-site profile under the real collision and landing authority; no retired integer
deck tier remains an opening authority.

### 7.2 State machine

The legal mission transitions are:

| From                     | Event                                   | To                       |
| ------------------------ | --------------------------------------- | ------------------------ |
| `preflight`              | `START`                                 | `flying`                 |
| `flying`                 | safe target-platform contact            | `landed`                 |
| `flying`                 | unsafe concrete swept contact           | `crashing`               |
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

Safe contact performs one indivisible service preparation before `landed` renders: for sites
`0..4094`, generate and prove the next site; mark the contacted can collected; add its award;
increment `completedSites`; derive and store the next `refuelRatio` exactly once; and freeze upright
at the deck. If ordinary next-site generation hits its invariant error, none of these mutations
commit and section 5.3's distinct non-restartable error result applies.

Site `4095` is the sole terminal transaction. It does not call `createSiteForIndex(4096)`, select or
replay a route proof, or create a descriptor beyond the physical rail. It otherwise preserves the
ordinary can, 300 ms refuel, 900 ms deployment, 1,400 ms power, installed agent, status, and
launch-ready transitions. Its sufficient terminal allowance is exactly `B=12.65`, the section 10.3
formula with an absent next leg represented only here as `deckDelta=0`; the committed award is
`12.65*currentRefuelRatio`, added to carryover without rounding or clamp. The same transaction sets
`activeSiteId=4095`, `targetSiteId=null`, `targetRouteProof=null`, leaves `generatorCursor=4096`,
and increments `completedSites` to `4096`. The target cue is hidden. After deployment the player may
launch, explore the remaining `46.2 m`, reverse, or contact the visible right terminus. No new
outcome state, copy, control, route, or automatic Exit is introduced. Normal timing for both
ordinary and final services is:

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
powered-base progress, next-base progress, and award authority. At the final checkpoint the two
target fields are exactly null and `generatorCursor=completedSites=4096`. Restore copies them
literally; it does not iterate a recurrence or recompute them from mutable post-crash state. A model
invariant independently verifies `refuelRatio===refuelRatioForBase(completedSites+1)` at snapshot
creation and restore, so a mismatched snapshot fails rather than drifting.

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
angularTravel = angularVelocity * dt
x += vx * dt; y += vy * dt; angle = normalizeDegrees(angle + angularTravel)
```

Here `left` and `right` are the effective post-assist, post-fuel values, while `s` remains the
pre-assist manual intent. Both engine forces use the same `delta`; their sum therefore has a real
lateral component and a forward component reduced by `cos(delta)`. The post-fuel engine difference
drives torque, so partial exhaustion proportionally reduces both translation and rotation. There is
no environmental drag, damping, bounce, random force, variable mass, position rounding, or doubled
gravity. Coasting in vacuum preserves velocity and angular velocity exactly apart from gravity; only
the explicitly fueled neutral-collective assist arrests rotation. `integratePose` returns the exact
unwrapped `angularTravel` with its normalized next pose so section 9 never reconstructs physical
rotation by shortest-arc subtraction. Player flight, manual departure, and reference schedules share
`effectiveThrust` and `integrateStep`.

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

The fixed-step integrator has endpoint authority only: it produces the previous and next poses plus
the exact unwrapped angular travel for that step, but defines no physical curve, shortest-angle
interpolation, or other state between those endpoints. Section 9's affine slabs are the sole
canonical within-step simulated sweep used for collision. Normalizing the displayed endpoint angle
never replaces the unwrapped travel. Set `COLLISION_MARGIN=.02 m`, `COLLISION_ANGLE_KNOT_DEGREES=1`,
and `R=hypot(1.6,6.5)=6.694027188471824`. This authority changes no fixed-step endpoint pose,
velocity, control, fuel, or rendered endpoint.

After broad-phase pruning, stream an ordered knot sequence containing `t=0`, `t=1`, and the
normalized times at which `previous.angle+angularTravel*t` crosses each integer degree in the
unwrapped direction. Section 5.4 bounds the sequence at 50,828 entries: an interval of length
`50825.333333333336` degrees crosses at most 50,826 integer degrees, plus its two endpoints.
Enumerate only integer degrees strictly between the endpoint angles: for positive travel the
inclusive integer range is `floor(previousAngle)+1 .. ceil(nextUnwrappedAngle)-1`; for negative
travel it is `ceil(previousAngle)-1 .. floor(nextUnwrappedAngle)+1`, visited downward. Then append
`t=1`. Retain only the previous and current knot hulls plus the current slab's left-first stack;
never allocate an array proportional to the knot count. At every knot, linearly interpolate the
center between the two physics endpoints and call the ordinary hull transform once with that knot's
unwrapped angle to obtain four Number vertices. Between adjacent knots, parameterize each
corresponding transformed vertex affinely in local `u in [0,1]`. Those four affine vertex paths are,
by definition, the only continuous within-step hull for that slab. They remain convex because the
angular difference is at most one degree, meet byte-identically at every shared knot, and equal the
ordinary hull at both fixed-step endpoints. There is no competing circular, trigonometric,
pose-angle, or rendered interpolation between knots. A `181`, `360`, `721`, or
maximum-reachable-degree step therefore enumerates every simulated turn in order rather than taking
a shortest arc.

Collision terrain is procedural authority inside section 5.4's closed world, not retained render
data. Derive every canonical 16 m segment intersecting the full-step X enclosure directly from
`(seed,signedVertexIndex)`, clamping only to the exact terminal indexes `-24576..24576`. Retained
terrain vertices remain a bounded DOM projection and are never a collider boundary. Add both
terminus rays whenever the enclosure reaches their inner faces. Structures are deliberately
different: collision considers exactly the authoritative maximum-three retained site
descriptors—active, target, and preceding—and never synthesizes future or evicted sites. From each
retained descriptor, derive its platform, continuous truss envelope, all three column envelopes, NOC
body, and solid `.5 m` mast from section 5.3, never from controller DOM coordinates.

Before constructing knots, the closed full-step hull enclosure is `[min(x0,x1)-R,max(x0,x1)+R]` by
`[min(y0,y1)-R,max(y0,y1)+R]`. In-domain terrain has exact top `29.2`; retained structure tops are
descriptor-derived. If the enclosure excludes both terminus faces and its bottom is greater than the
maximum ordinary candidate top plus `COLLISION_MARGIN`, return clear in O(1) without generating a
vertex or knot. Closed-enclosure tests likewise remove every structure, terrain segment, and
terminus whose expanded X/Y range is disjoint. A large high sweep wholly inside the rails is O(1). A
low or rail-crossing sweep performs the finite work needed for every intersected procedural segment
and unwrapped degree; no work count, speed, time, or subdivision limit changes its result.

Traverse each remaining knot slab with a left-first stack. For local interval `[a,b]`, compute the
maximum Euclidean displacement of its four affine vertices. Its Number midpoint is guaranteed to be
strictly internal: section 5.4's bounds require at most 20 halvings before displacement is
`<=COLLISION_MARGIN`. Discard an interval only when the closed union of its endpoint hulls expanded
by that displacement is disjoint from the candidate. Otherwise push right then left until the
displacement limit is met. At an accepted leaf, sample its two endpoints and midpoint against the
candidate expanded by `.02 m`. These samples are candidate detection only. If the unexpanded hull
has any real contact inside a leaf, a leaf endpoint is within `.02 m` because every hull point moves
at most `.02 m`; therefore the expansion cannot miss actual contact. Conversely, an expanded sample
never itself causes a crash.

For each detected leaf, reconstruct contact against the unexpanded terrain segment, structure
polygon, or terminus ray. A slab hull edge has affine endpoints. Substituting it and one fixed
feature edge into the closed segment-intersection orientation and parameter equations yields only
linear or quadratic binary-rational polynomials in local `u`. Losslessly decompose their Number
coefficients into signed `BigInt` significands and powers of two. Exact coefficient-zero and
quadratic-discriminant signs distinguish no root, two crossing roots, one repeated stationary
tangent, and an identically collinear equation. Resolve the collinear case by the corresponding
affine closed-projection overlap. Simple roots use exact dyadic sign isolation; a zero discriminant
uses its exact `-b/(2a)` root. `BigInt` numerator/exponent midpoints always progress. The finite
coefficient bit lengths give the ordinary quadratic root-separation bound, so unequal candidates
become disjoint; an exact degree-at-most-two resultant identifies a shared root for precedence.
Closed parameter constraints are evaluated with the same rational signs.

Process leaves and roots in increasing time and use the existing equal-time feature precedence. The
first reconstructed unexpanded intersection is the contact authority. Refine its rational interval
until it uniquely rounds to one ties-to-even JavaScript Number `t`; the returned pose is the
ordinary Number projection at that time, while the linear/quadratic equation, isolating interval,
feature IDs, and exact constraint signs prove a real contact occurred. The classifier need not
retain that certificate in mutable game state, but permanent independent tests reconstruct it. A
near miss or expansion-only candidate is clear. No recursion cutoff, deadline, coefficient error,
failed isolation, enclosure, or resource budget becomes contact.

The scaffold broad-phase polygons conservatively cover the exact truss and three column envelopes
rather than pretending each narrow member is a separate passable collider. Section 5.3's
member-width, native-foot, and aperture-diameter proofs are the required honesty conditions for that
conservative treatment. Tests independently reconstruct every rendered member segment, clear
aperture envelope, and closed collision polygon; apply the exact butt-cap/round-join stroke
geometry; reject any rendered point outside its `+/-MEMBER_HALF` expansion; and reject any aperture
diameter at or above the rigid hull width.

Treat the target platform's top face separately so its upward normal is never margin-expanded into a
valid approach. On each affine knot slab, hull-vertex height minus `platformTop` is linear and every
transformed hull-edge/top-segment intersection constraint is degree at most two. Feed those
polynomials through the same exact linear/quadratic authority. A repeated root is a stationary
tangent; an exact root outside the closed top span is discarded, and a near miss has no root. The
platform ends, underside, truss, and columns remain unexpanded unsafe geometry; their `.02 m`
expansions remain candidate detection only. Only an actual isolated top contact proceeds to the
safe-envelope test.

Collect actual unsafe and target-top roots from the first contact slab, order them by exact root
comparison, and use unchanged exact-time precedence: NOC or mast, non-top platform surface, unified
truss or any column, terminus, terrain, then target platform top. The target top is safe only when
both transformed feet are on its closed `9.6 m` span, neither hull side intersects an end, `vy<=0`,
and these inclusive limits hold at contact:

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

Delete the active-site `center-45`, target-site `center+65`, initial `-5`, fallback `101`, and every
generic horizontal `"bounds"` failure path. Crossing any former value, passing a target without
touching it, reversing, and returning inside section 5.4's physical rails are ordinary flight and do
not complete, fail, or mutate site progress. Delete `MAX_PLAYABLE_Y`, the `"ceiling"`,
`"overspeed"`, and cutoff-derived `"grazing"` causes, the 64-interval ceiling, and every model
transition that treats finite travel or altitude as contact. At zero fuel the effective engines and
vector angle remain zero and gravity continues ballistically. Only a concrete contact returned by
the complete swept authority above can crash. The fixed scene may clip a high or distant lander and
never follows it vertically, but clipping, a retained render-window edge, missing DOM terrain, and
absence of a retained future/evicted structure are not contact or failure. A terminus failure cause
is exactly `"terminus"` and is emitted only with the isolated contact root for the visible left or
right physical rail; feature identity retains the side for independent reconstruction, not for a
second user-facing cause or string.

## 10. Bounded route proof and progressive award

### 10.1 Deterministic bounded corridor synthesizer

Phase 4S retains one deterministic offline corridor-route synthesizer and checked proof records. It
is derivation tooling, never a runtime planner. Production performs one keyed lookup by the exact
ordered endpoint-pair key and one exact replay; it does not scan templates, search commands, read
carried fuel, reject terrain profiles, or retry another seed.

Phase 4S plan language that preserves route behavior means the 96 m spacing, forward target
sequence, player controls, runtime lookup/replay behavior, and mission lifecycle remain immutable.
The generated proof schedules are evidence rather than player-visible routes and may change only as
needed to certify the new terrain geometry, exactly as the lead-owned Phase 4S plan states.

All synthesis arithmetic is JavaScript Number arithmetic using the section 8 integrator at exactly
120 Hz. Candidate fuel starts at `30`; fuel is a search cost only because every accepted schedule
burns less than the section 10.3 allowance. The canonical expansion order and pre-assist engine
requests are `0:(0,0)`, `1:(.72,.72)`, `2:(0,.375)`, `3:(.375,0)`, `4:(.2125,.5875)`, and
`5:(.5875,.2125)`. Duplicate reachable zero/straight keyboard rows are never expanded. A pair search
starts from the exact relative checkpoint `(x,y,vx,vy,angle,angularVelocity)=(0,0,0,0,0,0)`,
`fuel=30`, with the origin platform's launch hold active. Every candidate begins with this exact
1,087-step mast-clearing prefix:

```text
commands  1,  0, 1, 0, 2,5, 3,  0, 2,  0,  1,  0, 2
counts   90,218,21,94,12,6,41,119,23,180,129,120,34
```

Relative to the origin center/deck, its exact terminal state is
`(x,y,vx,vy,angle,angularVelocity)=(9.369360750429012,11.213226284049306,4.16044150783088,1.3150456371170334,18.306988062798155,-6.99503284726514)`;
burn is `3.2637500000001936`. Exact replay, including the origin-top exception only until both feet
clear by `.05 m`, remains authoritative for the prefix on every geometry.

The four phases are exact: `launch` is prefix steps `1..1087`; `corridor` is macro layers `1..190`;
`capture` is layers `191..210`; and `descent` is layers `211..269`. The negative-delta desired-Y
override below may begin during `corridor`, but it does not change the phase boundary, X waypoint,
command vocabulary, or pruning authority. A retained beam state contains exactly the full-precision
six-component relative pose, remaining candidate fuel, maximum absolute hull top so far, and merged
run sequence; its layer and absolute fixed step are loop indexes, not mutable state fields.

Set `MACRO_STEPS=12`, `MAX_LAYERS=269`, and `BEAM_WIDTH=6000`; therefore the final expanded substep
is absolute fixed step `1087+269*12=4315`. For every beam state, advance each command for exactly 12
ordinary fixed steps. Track the extrema at every substep. Reject a non-finite state, candidate fuel
below zero, minimum reference Y below `min(-.5,deckDelta-.5)`, X outside `[5,107]`, or reference Y
below `envelope(x)-originDeck+1.65`. While `84<x<91.2`, also require `y>=deckDelta+11`; this is the
target-approach barrier. These are candidate-pruning bounds only. They never replace rotated-hull,
terrain, structure, or landing classification in exact replay.

Quantize only for beam deduplication:

```text
key = [
  Math.round(x*2), Math.round(y*2), Math.round(vx*2), Math.round(vy*2),
  Math.round(angle/5), Math.round(angularVelocity/5)
].join(":")
```

Fuel, cost, and the full-precision pose remain on the retained state. For a duplicate key, retain
the least tuple `(cost,-fuel,runs,pose)`; `runs` compares its flattened `[command,count,...]`
integer sequence lexicographically, then pose compares `x,y,vx,vy,angle,angularVelocity` as raw
Numbers. After deduplication, sort by that same tuple and retain the first 6,000. Expansion iterates
the already-sorted beam outermost and commands in numeric order. Adjacent equal commands merge
immediately, so every run count is positive and adjacent command indexes differ. These explicit
comparisons replace reliance on map insertion order or sort stability.

Let `l` be the one-based macro layer, `delta=deckDelta`, `cruiseY=max(11.5,delta+11.5)`, and
initialize desired `vx=4.2,vy=0`. The exact waypoint authority is:

```text
l <= 190:
  x = prefixX + (90-prefixX)*l/190
  y = 11.5 + (cruiseY-11.5)*min(1,l/120)
190 < l <= 210, q=(l-190)/20:
  x = 90+4*q; vx = 4.2-3.2*q; y = cruiseY
l > 210, q=min(1,(l-210)/50):
  x = 94; vx = 1-q
  y = cruiseY-(cruiseY-(delta+.7))*q; vy = -1.5
```

For `delta < -10`, override desired Y with `cruiseY+(delta+.7-cruiseY)*clamp((l-120)/140,0,1)` and,
before the endpoint, desired VY with `(delta+.7-cruiseY)/14`. This early descent is safe because the
target barrier top is at relative Y `delta+11<=1`, below the corridor-safe trajectory. For
`-10 <= delta < 0`, retain the ordinary corridor desired Y/VY until the candidate's full-precision
post-layer reference X satisfies `pose.x>=b=91.2`; only then use the same expressions with
`clamp((l-150)/110,0,1)` and divisor `11`. This exact geometry-derived predicate keeps a
shallow-fall candidate above the target barrier and NOC envelope until it has cleared their strict-X
boundary. `delta=-10` belongs to this shallow branch; there is no epsilon or rounded comparison. At
the endpoint desired VY is `-1.5`.

For state `p` after layer `l`, define `terminalBias=max(0,l-210)*.08` and exactly:

```text
cost =
  1.8*abs(p.x-desiredX) + 2.4*abs(p.y-desiredY)
  + (2+terminalBias)*abs(p.vx-desiredVx)
  + (2+terminalBias)*abs(p.vy-desiredVy)
  + .045*abs(p.angle) + .035*abs(p.angularVelocity)
  + .08*(30-fuel)
```

A terminal candidate has `x in [92.8,99.2]`, `y in [delta,delta+.3]`, `abs(vx)<=2.2`, `vy<=0`,
`abs(vy)<=3.6`, `abs(angle)<=18`, and `abs(angularVelocity)<=26`. After each layer's 6,000-state
truncation, examine its terminal states in beam order. Append command 0 only through fixed step
4,320, merge it with an adjacent zero run, and replay from the exact checkpoint with the reviewed
allowance. The first exact safe target contact is the selected record; trim its runs at that first
contact. Stop after the first layer with a selected record. A contact after 4,320, any earlier
unsafe contact is not a candidate. If no layer succeeds, derivation fails. Hull top is recorded but
has no acceptance ceiling.

Before residual synthesis, v9 consumes the checked
`website/tests/fixtures/lander-route-derived-v7.json` as one explicit immutable bootstrap input. Its
complete file bytes, including the single trailing LF, must hash to
`c5800497182045dbf664fd50abd6cfd79cc4293bdadbfd4afa526e72f7d71b12` with SHA-256. The parsed fixture
must have schema `agw-lander-route-derived/v7`, `outputDigest`
`dea7263fe5b01ea1c0a442a1f2fefb3f4dad472cbe8668b8bf00793cde5afef7`, `proofDigest`
`ca09ed720e3e752745af046cbb2013c99c36227963e9799b1f1cd8961b49f354`, and exactly 100 records; any
byte, schema, digest, or count mismatch fails before derivation. This fixture is permanent reviewed
input, not a disposable file, an implicit prior-Git dependency, or runtime authority.

From each bootstrap record read only `pairKey`, `runs`, `scheduleDigest`, and
`search.selectedLayer`. Recompute the FNV-1a digest from `runs`, require it to equal
`scheduleDigest`, and reject malformed, empty, adjacent-equal, non-positive-count, unknown-command,
or over-4,320-step runs. Deduplicate only byte-identical canonical `runs`; for a duplicate retain
the source having the numerically least `(originMillimeters,targetMillimeters)` parsed from
`pairKey`. Sort distinct candidates by unsigned numeric `scheduleDigest`, then that same numeric
source-pair tuple; the pinned bootstrap yields exactly 75 distinct candidates. For each new
numeric-sorted v8 key, independently exact-replay every candidate in this order against the key's
conservative envelope. The first safe replay in the tuple
`(scheduleDigest,sourceOriginMillimeters,sourceTargetMillimeters)` is selected and then replayed
against every concrete member assignment. Its new record stores
`search={selectedLayer:source.search.selectedLayer,macroExpansions:0,terminalReplays:n}`, where `n`
is the one-based count of bootstrap candidates replayed through the selected safe result. An unsafe
or incomplete replay continues to the next candidate; no approximate clearance, profile read,
carried-fuel read, or source-record success claim is trusted. Only a key with no safe bootstrap
candidate enters the synthesizer below.

The deterministic residual-synthesis ceiling per pair is 269 layers, 6 commands, 6,000 retained
states, at most 9,684,000 macro expansions, and at most 1,614,000 exact terminal replays. No random
restart, wall-clock cutoff, worker-dependent merge, recipe widening, or early acceptance before
exact replay exists. The schedule digest uses the existing FNV-1a
command/low-count-byte/high-count-byte fold. The v9 derivation tool independently implements the
integrator and complete swept-contact classifier and imports no production or test module. Reading
the explicit bootstrap data file through the pinned narrow projection above is not a module import.
Its selected replay is necessary but not sufficient: permanent model verification replays every
stored run against production `classifySweptContact` on both its conservative envelope and every
concrete member assignment. Independent and production classification, first-contact step, canonical
pose, burn, and hull maximum must agree before a record is usable. Runtime then performs the one
defensive production replay already required above.

For an ordered pair with origin deck `D0` and target deck `D1`, set `O=D0-2.5`, `T=D1-2.5`,
`a=13.8`, and `b=91.2`. Its exact conservative envelope is `O` for `x<=a`, `T` for `x>=b`, and
`min(29.2,O+.36*(x-a),T+.36*(b-x))` between them. Construct its strict-X polyline from `a,b` and
every in-range intersection of those three affine branches; do not sample it on an approximate grid.
Store `envelope` as increasing-X `[x,y]` pairs clipped to `[5,107]`, including those endpoints, `a`,
`b`, and every active-branch intersection, then remove only adjacent byte-identical points. Every
eligible terrain obeys the `.36` absolute-grade bound and is no higher than `O/T` across the two
closed footprints, so it is at or below this envelope at every X. The proof-equivalence key is the
exact ordered deck pair plus this derived envelope. Derived v8 retains the exhaustive
assignment-to-key mapping, and verification replays every concrete assignment as well as its
envelope.

The disposable feasibility corpus covers all 243 keys without filtering: 41 use the deep-fall
branch, 78 use the shallow-fall branch, and 124 are flat or rising. No key has exactly `delta=-10`,
but the inclusive shallow tie remains mutation-protected. Exact production replay accepts 208 keys
with 33 distinct bootstrap corridor schedules; the same deterministic recipe synthesizes and exactly
replays all 35 residual geometries. Across the complete 243-record witness, maximum target-contact
step is `4,247`, maximum controller burn is `12.81125000000022`, maximum swept hull top is
`53.4231353132934 m`, and maximum base burn after subtracting only the positive-delta potential
surcharge is `12.604750000001879`, which quantum-ceils to the unchanged `B=12.65`. Residual
synthesis uses at most 9,098,124 macro expansions, one terminal replay, and selected layer 262, all
within the declared ceilings. Permanent v9 derivation must synthesize or independently replay every
record from declared inputs, then reproduce counts and maxima at least as strict as these witnesses;
it may not import disposable files or any undeclared prior output. The immutable v7 bootstrap above
is the sole permitted prior derived-data input and contributes schedules only through its pinned
narrow projection. A mismatch is a feasibility failure, not permission to relax collision, terrain,
or controls.

### 10.2 Opening proof, fixture authority, and digests

The initial approach remains exactly `(x,y,vx,vy,angle,angularVelocity)=(30,32,.8,-.4,0,0)` with
fuel `15`. Its proof world sets `originSiteId=null`: the craft has not departed a checkpoint, so the
origin-platform top exception is disabled throughout opening replay. Exact swept replays cover all
eight possible site-0 profiles rather than conditioning terrain around the spawn:

| Profile | Deck top | Off/on fixed steps | Contact step | Fuel remaining |
| ------- | -------- | ------------------ | ------------ | -------------- |
| S0      | 26.228   | 162 / 72           | 434          | 14.136         |
| S1      | 11.988   | 348 / 102          | 569          | 13.776         |
| S2      | 8.852    | 378 / 114          | 621          | 13.632         |
| S3      | 19.612   | 264 / 72           | 434          | 14.136         |
| S4      | 25.732   | 168 / 72           | 438          | 14.136         |
| S5      | 9.46     | 378 / 102          | 561          | 13.776         |
| S6      | 19.972   | 258 / 72           | 435          | 14.136         |
| S7      | 8.852    | 378 / 114          | 621          | 13.632         |

The schedules are test-only witnesses, not runtime input. Static seed `0x41475731` selects S0, a
`26.228 m` site-0 deck, and passes the S0 witness. No spawn-relative terrain or deck adjustment
exists.

The independent assignment enumeration is exact and finite. Use abstract origin-center phases in
numeric order `[4,36,68,100,132,164,196,228,260,292,324,356,388,420,452,484]`. Let
`leftBlock=floor((phase-4.8)/512)` and `rightBlock=floor((phase+109.8)/512)`. When these are equal,
enumerate profile IDs `0..7` once and set both `leftProfile` and `rightProfile` to that ID. When
they differ, enumerate `leftProfile=0..7` outermost and `rightProfile=0..7` innermost, skipping
equality because section 5.2 forbids adjacent repeats. This produces exactly `12*8+4*8*7=320`
assignments in canonical order. Encode each ID exactly as `p<phase>-a<leftProfile>-b<rightProfile>`,
without zero padding; for example `p4-a0-b1`.

For each assignment independently evaluate both closed footprints centered at `phase` and
`phase+96`. Convert an exact deck to integer millimeters with `Math.round(deck*1000)` and assert the
round trip differs by at most `1e-12`; no rounded millimeter value feeds terrain or collision.
Encode the proof key as `d:<originMillimeters>:<targetMillimeters>`. Group assignments only by that
key and section 10.1's deterministically derived envelope. Sort keys by numeric origin millimeters,
then numeric target millimeters. The result must be exactly 243 keys and 224 distinct signed deltas,
with per-phase key counts `[17,8,8,8,8,8,8,8,8,8,8,8,8,35,50,45]`. Each record's membership array
retains assignment IDs in canonical enumeration order.

Each assignment descriptor contains exactly these fields:

```text
assignmentId,phase,leftBlock,rightBlock,leftProfile,rightProfile,originDeck,targetDeck,
originMillimeters,targetMillimeters,deckDelta,pairKey
```

Each proof record contains exactly:

```text
pairKey,envelope,assignmentIds,assignmentMembershipDigest,runs,scheduleDigest,
search,success,controllerBurn,baseBurn,climbSurcharge,allowance,maxHullTop
```

`search` contains `selectedLayer,macroExpansions,terminalReplays`. `selectedLayer` is one-based. For
a residual-synthesis record, `macroExpansions` counts every attempted 12-step child before pruning
or deduplication and `terminalReplays` counts every post-truncation terminal candidate sent to exact
replay. For a bootstrap-selected record, the exact zero/count/source-layer meanings in section 10.1
apply. `success` contains `classification,contactStep,pose`. Each opening contains exactly
`profile,deck,runs,contactStep,pose,burn,reserve,classification`. Object key order is immaterial
only because canonical JSON sorts it; no optional field, null placeholder, duplicated derived value,
or prose label is permitted.

Bump the permanent inputs to the immutable bootstrap fixture `agw-lander-route-derived/v7` plus
geometry schema `agw-lander-route-geometry/v8`, derived schema `agw-lander-route-derived/v8`,
deriver `agw-lander-route-deriver/v9`, collision recipe `agw-lander-swept-collision/v2`, and the
unchanged synthesizer recipe `agw-lander-corridor-synthesizer/v1`. Geometry v8 preserves v7's
top-level and structure objects, replaces `terrain` with exact keys
`cadence,epochSuperblocks,gradeChangeLimit,gradeLimit,mapping,profiles,selection,superblockWidth`,
sets `epochSuperblocks=8` and `superblockWidth=512`, and gives `selection` exactly
`{firstAdvancePerEpoch:1,lastOffset:2,offsetStream:15,shuffleOrder:[5,4,3,2,1],shuffleStream:16}`.
It contains the exact eight profiles, 16 m lattice, normalized mapping, grade/change bounds, 96 m
signed site formula, closed-footprint maximum, exact 2.5 m clearance, and unchanged structure
inputs. Geometry v8 also adds exact `world`
`{maxSiteIndex:4095,maxX:393216,minSiteIndex:-4095,minX:-393216,terminusWidth:.2}` and collision
`{angleKnotDegrees:1,margin:.02,recipe:"agw-lander-swept-collision/v2"}` objects. Derived v8
contains the ordered 320 assignments, 243 proof records, eight opening records, one terminal record,
`canonicalPoseDecimals:9`, all digests below, and independent signed 100-site witnesses. Record
poses are canonicalized only for stored proof output, after exact classification, with
`Number(value.toFixed(canonicalPoseDecimals))`; the raw pose used by burn, contact, collision, and
selection is never rounded, nor are deck or terrain values. Digests consume the stored canonical
record and otherwise raw values exactly as scoped below. Records are ordered by numeric proof key.
Openings are ordered `S0,S1,S2,S3,S4,S5,S6,S7`. World witnesses are ordered seed outermost in
`[11,39,41,STATIC_WORLD_SEED]`, direction `[-1,1]`, then ordinal `0..100`; site index is
`direction*ordinal`. Each site-only witness contains exactly `descriptor,digest`, and `digest`
hashes only its complete descriptor. A descriptor contains exactly
`seed,siteIndex,directionlessPhase,superblocks,site`, where
`directionlessPhase=positiveModulo(C_siteIndex,512)`. `superblocks` contains every signed superblock
intersecting the site's closed footprint, once and in ascending index order; each entry contains
exactly `epoch,index,profile,slot,vertices`, with vertices in increasing X. `site` contains exactly
`index,center,closedFootprint,localNativeMaximum,platformTop,supportFeet`, with the footprint in
left/right order and the six feet in section 5.3 rail order. Route records separately cover every
ordered endpoint pair. The duplicated ordinal-zero direction witnesses must therefore be
byte-identical; direction is an enumeration instruction and is not stored in the directionless
site-0 descriptor. The terminal record contains exactly
`siteIndex,deckDelta,allowance,ratio,award,completedSites,generatorCursor,activeSiteId,targetSiteId,`
`targetRouteProof,cueDirection`; its independently replayed vector is
`4095,0,12.65,1,12.65,4096,4096,4095,null,null,null`. This late-run ratio is the existing exact
Number projection and the award remains formula-derived rather than a second literal in production.
The derived top-level payload contains exactly

```text
schema,deriverVersion,synthesizerVersion,collisionVersion,canonicalPoseDecimals,
bootstrapDigest,geometryDigest,physicsDigest,assignments,assignmentDigest,records,openings,terminal,
proofDigest,worldWitnesses,worldDigest,outputDigest
```

Canonical JSON recursively sorts object keys, preserves the pinned array orders, uses compact
`JSON.stringify`, and hashes UTF-8 bytes with lowercase SHA-256. Hash payloads never include a
trailing LF; fixture files add exactly one unhashed trailing LF. Digest scopes are exhaustive:

- `bootstrapDigest` is the lowercase SHA-256 of the complete immutable v7 bootstrap file bytes,
  including its one trailing LF; it must equal
  `c5800497182045dbf664fd50abd6cfd79cc4293bdadbfd4afa526e72f7d71b12`.
- `geometryDigest` hashes the complete geometry-v8 input object.
- `physicsDigest` hashes exactly `{collisionVersion,commands,constants}`. `commands` is the
  unchanged ordered eight-row pre-assist table. `constants` contains exactly these sorted names:
  `ANGULAR_ASSIST_DIFFERENTIAL`, `ANGULAR_ASSIST_FULL_SPEED`, `COLLISION_ANGLE_KNOT_DEGREES`,
  `COLLISION_MARGIN`, `ENGINE_ACCELERATION`, `FUEL_FLOW`, `FUEL_QUANTUM`, `GRAVITY`,
  `MAX_LANDING_ANGLE`, `MAX_LANDING_ANGULAR_SPEED`, `MAX_LANDING_DESCENT_SPEED`,
  `MAX_LANDING_HORIZONTAL_SPEED`, `MAX_THRUST_VECTOR`, `STEP_SECONDS`, `TERMINUS_WIDTH`,
  `TORQUE_ACCELERATION`, `TURN_DIFFERENTIAL`, `TURNING_TOTAL`, `WORLD_MAX_X`, and `WORLD_MIN_X`,
  with their section 5.4 and 8-9 values.
- each `assignmentMembershipDigest` hashes only that record's ordered assignment-ID array;
  `scheduleDigest` remains the section 10.1 unsigned FNV-1a fold over its ordered runs.
- `assignmentDigest` hashes the complete ordered assignment-descriptor array.
- `proofDigest` hashes exactly `{records,openings,terminal}` after pose canonicalization.
- `worldDigest` hashes exactly the complete ordered `worldWitnesses` array.
- `outputDigest` hashes the complete derived-v8 object with only `outputDigest` omitted, so it
  covers schemas/versions/precision, all preceding digest literals, assignments, records, openings,
  the terminal record, and world witnesses.

The independently generated assignment-authority digest remains available at design time because
termini do not alter terrain, sites, or the 320-member closure:

```text
assignmentDigest 597c7ffe9cba6ce2d95dd1204e40ff8562bea22088ca13fa9b930656992a918b
```

The pinned bootstrap digest and corrected geometry, physics, proof, world, and output digests must
be emitted together by the first independent v9 derivation, reviewed as one atomic fixture change,
and then copied into the generated proof projection. The pre-correction Phase 4R/4S literals are
forbidden; the LLD does not invent pre-derivation output hashes. The fixture remains primary
authority. Verification recomputes every digest and fails on partial regeneration, using v7 as the
primary output or verification target, any v7 read outside the pinned bootstrap projection, missing
assignment/pair/profile/terminal authority, ordering drift, payload-scope drift, or changed
collision/physics digest.

The exact CLI remains standard-library Node and accepts only:

```text
node website/tools/derive_lander_routes.mjs \
  --geometry website/tests/fixtures/lander-route-geometry-v8.json \
  --bootstrap website/tests/fixtures/lander-route-derived-v7.json \
  --output PATH [--verify PATH]
```

Unknown or missing flags exit 2; derivation or verification failure exits 1; success exits 0.
Ordinary tests verify checked canonical data and never regenerate their expectations. Tooling never
ships in the 13-file site artifact.

The source-projection CLI is exact:

```text
node website/tools/project_lander_route_proofs.mjs \
  --fixture website/tests/fixtures/lander-route-derived-v8.json \
  --output website/static/lander-route-proofs.generated.js
```

It accepts no other argument order or flag, writes atomically, preserves numeric-sorted fixture
record order, and emits one compact row per record after the exact shared prefix. Each row orders
`pairKey,path,tailCommand,tailCount,scheduleDigest,contactStep,x,y,vx,vy,angle,angularVelocity,`
`controllerBurn,baseBurn,climbSurcharge,allowance,maxHullTop,assignmentMembershipDigest,envelope,`
`selectedLayer,macroExpansions,terminalReplays`. The projection then exports only `ROUTE_DIGESTS`,
`REFERENCE_PROOFS`, and the direct-key `REFERENCE_PROOF_CATALOG`. Permanent tests rerun this
projection to a temporary path and require byte equality with the reviewed generated source; build
tests mutation-kill a missing/renamed import or provenance marker and prove the final artifact still
has exactly 13 entries.

`ROUTE_DIGESTS` contains exactly `assignmentDigest,bootstrapDigest,geometryDigest,outputDigest,`
`physicsDigest,proofDigest,worldDigest`, copied from derived v8 without recomputation by the
projection tool.

### 10.3 Honest sufficient allowance and award

Set `FUEL_QUANTUM=.05` and define `quantumCeil(x)=ceil(x/FUEL_QUANTUM)*FUEL_QUANTUM` with no epsilon
subtraction or integer-key workaround. For each proof record let `delta=targetDeck-originDeck` and
`climbSurcharge=max(0,delta)*GRAVITY/ENGINE_ACCELERATION=max(0,delta)/3`; descent receives no
credit. In each record, `controllerBurn` is the raw exact replay burn,
`baseBurn=controllerBurn-climbSurcharge`, `climbSurcharge` is the expression above, `allowance` is
the expression below, and `maxHullTop` is the greatest absolute transformed hull Y observed at any
swept substep through first contact. The corpus-wide controller base is exactly:

```text
B = quantumCeil(max over 243 records of
    (successfulControllerBurn - max(0,deckDelta)/3))
  = quantumCeil(12.604750000001879)
  = 12.65
allowance(deckDelta) = quantumCeil(B + max(0,deckDelta)/3)
```

This is an honest sufficient allowance, not a schedule-specific minimum and not a global physical
optimum. Do not add fuel-wasting thrust, loitering, or a manufactured failure merely to consume it.
Ordinary sites `0..4094` continue to use their proved next-deck delta. At final site `4095` only,
section 7.2 supplies `deckDelta=0`, so `allowance=12.65`; this is the terminal refuel authority and
not a route-success claim or a generated schedule. Phase 4S retains the retirement of both
`smallerFailure` and the claim that `allowance-.05` must make the route impossible. A separate
mutation-sensitive engine-arithmetic test starts with a deliberately bounded request/reserve, proves
exact partial-step exhaustion and zero subsequent thrust, and pins quantum rounding at both sides of
every allowance boundary; it makes no route-impossibility claim.

A runtime proof record contains its exact pair/envelope key, assignment-membership digest, schedule
digest, runs trimmed at first contact, contact step/pose/burn/classification, maximum hull top, base
burn, climb surcharge, and sufficient allowance. Runtime performs exactly one keyed record lookup
and one defensive exact replay from the centered upright checkpoint. Replay uses the reviewed
allowance and must reproduce safe contact and every stored witness within the existing numeric
tolerance. Any mismatch takes the existing transactional `generation-error` path; there is no second
replay, search, fallback record, relaxed envelope, lower terrain, or fuel scan.

For one-indexed successfully powered base number `n`, retain exactly `ratio(n)=1+0.5**(n-1)` and all
existing finite-precision vectors. On safe contact, validate the stored ratio, compute
`award=allowance(deckDelta)*ratio`, add it to carried fuel without rounding or capacity clamp,
increment progress once, and store `ratio(n+1)`. The gas can, checkpoint, proof, award, gauge
reference, and mission transition still commit atomically. Different carried reserves never alter
the proof or allowance; only the already-established ratio changes the award. The opening reserve
still funds site 0, and site 0's can funds leg 1.

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
block-start border, `block-size:48px`, and responsive padding. Its exact layout authority is a
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
`.4rem`. Its block size becomes exactly `84px`. Exit remains `justify-self:end`. The two fixed
40-character lines therefore receive the full rail content width rather than competing with the
action column. At 320 CSS pixels and the 400-percent-zoom equivalent, the `25/16` stage keeps its
own geometry and the rail grows in normal flow without horizontal page overflow. The in-stage
outcome remains `min(32rem,calc(100% - 6rem))`, leaving the fuel gauge inset clear; its status and
optional Retry stack without overlap. The stage, gauge, status, Retry, each controls line, and Exit
computed rectangles must be pairwise disjoint except for intentional parent containment.

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

| Vector                 | Input                                                                                                                 | Expected result                                                                                                                                                              |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Gravity, 120 steps     | `(10,30,0,0)`, zero angle/engines, fuel 30                                                                            | `x=10`, `y=28.4875`, `vx=0`, `vy=-3`, fuel `30`                                                                                                                              |
| Collective, 120 steps  | Same pose, engines `(0.72,0.72)`                                                                                      | `y=35.0215`, `vy=9.96`, angle/x unchanged, fuel `28.56`                                                                                                                      |
| Turn-only vector       | One step from same pose, raw engines `(0,0.375)`, `s=-1`                                                              | `ax=-1.6875`, `ay=-0.07716426222751949`, `omega=-0.25`, angle `-0.00208333333333`, fuel `29.996875`                                                                          |
| Combined turn vector   | One step from same pose, raw engines `(0.2125,0.5875)`, `s=-1`                                                        | `ax=-3.6`, `ay=3.235382907247959`, `omega=-0.25`, angle `-0.00208333333333`, fuel `29.993333333333332`                                                                       |
| Angular assist         | One step, angle `0`, omega `15`, raw engines `(0.72,0.72)`                                                            | engines `(0.66,0.78)`, `s=0`, omega `14.92`, angle `0.124333333333`, fuel `29.988`; total thrust unchanged                                                                   |
| Vacuum coast           | One step, angle `0`, omega `15`, zero engines                                                                         | omega remains `15`, angle `0.125`, `vy=-0.025`; no translational or angular damping                                                                                          |
| Exhaustion             | Fuel `0.005`, one step, engines `(1,1)`                                                                               | Effective engines `(0.3,0.3)`, fuel exactly `0`                                                                                                                              |
| Pointer vectors        | Rightward normalized drag `m=0,0.5,1`                                                                                 | `(.72,.72)`, `(.65375,.46625)`, `(.5875,.2125)`; leftward values mirror exactly                                                                                              |
| Mixed input ceiling    | Keyboard collective plus pointer full right                                                                           | pointer owns `s=1`; engines `(.5875,.2125)`, total `.8`, never component-combined                                                                                            |
| Keyboard steer owner   | Keyboard left plus pointer full right                                                                                 | keyboard owns `s=-1`; engines `(.2125,.5875)`, total `.8`                                                                                                                    |
| Canceled steer owner   | Both keyboard steers plus pointer half right                                                                          | keyboard cancels; pointer owns `s=.5`; engines `(.65375,.46625)`, total `1.12`                                                                                               |
| Empty-fuel direction   | Fuel `0`, raw engines `(.5875,.2125)`, retained physics `s=1`                                                         | effective engines `(0,0)` and stored/rendered `commanded.vectorAngle=0`                                                                                                      |
| Plumes                 | `u=0,0.5,1`                                                                                                           | scales `0.08,0.54,1`; opacities `0.25,0.625,1`                                                                                                                               |
| First site             | Any normalized seed                                                                                                   | ID `0`, center `36`; one of eight global profiles; deck equals closed-footprint maximum plus `2.5` exactly                                                                   |
| Site progression       | Signed indexes `-4095..4095`; sixteen 96/512 m spatial phases                                                         | `C_i=36+96*i`; ordinary legs use one exact pair-key lookup; no terrain read for X, scan, retry, or fallback                                                                  |
| Physical termini       | Approach exact X `-393216` and `393216` from inside at low and high altitude                                          | permanent rails are visible first; first isolated rail contact crashes; retained range edges remain inert                                                                    |
| Final service          | Power site `4095` with ratio `1` and arbitrary carryover                                                              | award `12.65`; completed/cursor `4096`; active `4095`; target/proof/cue null; ordinary deploy/checkpoint/launch                                                              |
| Terrain continuity     | Retained range crossing chunks, both sites, and all twelve column-rail feet                                           | one strict-X chain; boundary Ys equal; two render paths; open stroke has no floor/vertical/closing segment                                                                   |
| Structure parity       | Static and dynamic site with platform top `p`                                                                         | one 18.6 m path; 14 fixed truss members plus exactly three bounded variable lattice columns                                                                                  |
| Truss envelope         | Relative chords `[-4.8,13.8] x [-1.1,-.35]`, member width `.2 m`                                                      | collider `[-4.9,13.9] x [-1.2,-.25]`; deck/NOC overlap; top remains `.25 m` below landing face                                                                               |
| Column envelopes       | rail pairs `0/1,8.8/9.8,17.6/18.6`; six native feet; top `p-.35`                                                      | each collider is the exact stroked axis-aligned box through its lower foot; all members contained and joined                                                                 |
| Aperture bounds        | Raw truss, `.8 m` lattice bays, and the native-slope wedge                                                            | diameters `3.1894356867634124`, `1.2806248474865698`, and at most `1.0628264204469138`, all below hull `3.2`                                                                 |
| Connected clear face   | Every independently split face for each pinned site descriptor                                                        | actual maximum axis-aligned envelope equals fixture fields; `hypot(width,height)<=3.1894356867634124`                                                                        |
| Opening gauge          | Fresh run `fuel=15`, `fuelGaugeReference=30`                                                                          | exact level `.5`, exact accessible reserve `15.0`; no cap or hidden extra fuel                                                                                               |
| Later gauge            | `fuel=37.5`, `fuelGaugeReference=50`, then checkpoint restore                                                         | level `.75`, level `ready`; restore reproduces both values and never caps fuel                                                                                               |
| Gauge contrast         | danger/caution/ready against `#20232a`; gauge level zero                                                              | ratios `5.068/8.584/8.243`; graphite boundary plus colored inset remain visible with zero-height fill                                                                        |
| Refuel projection      | pre-award level `.25`; normal landed time `0,.15,.299,.3 s`                                                           | levels `.25,.625,.9975,1`; one can follows the same linear progress and is absent after `.3`                                                                                 |
| Refuel CSS frame       | stage rect `(100,50,1000,640)`, can scene `(130,433)`, gauge rect `(120,70,16,112)`, `p=.25`                          | viewport can `(230,483)`, local endpoints `(130,433)` to `(28,76)`, transfer center `(104.5,343.75)`                                                                         |
| Transfer silhouette    | DPR 1, integer CSS-pixel center; computed `::after` plus paired on/off `20 by 22` crops                               | six pinned layers/sizes/positions/colors; probes hit every outer/inner part and `(0,0)`/`(19,21)` match baseline                                                             |
| Reduced refuel         | Same contact with reduced motion                                                                                      | full model/fuel text/gauge/checkpoint atomically; `refuel=null`, no transfer pseudo-element                                                                                  |
| Launch-ready hold      | 10 seconds zero or steer-only input after power                                                                       | centered pose, fuel, mission time, zero command, and status remain unchanged                                                                                                 |
| Manual departure       | Launch-ready plus Space/Up, either plus vi/arrow steer, pointer/touch hold, or eligible tap                           | every qualifying path uses the ordinary mixer; first step burns/integrates; `flying` starts only after `.05 m`                                                               |
| NOC stages             | Power sequence at `0,.2,.4,.6,.8,1,1.2,1.4 s`                                                                         | stages `0..7`: installed agent at stage 1, four bars, then three arches; banner only at final stage                                                                          |
| Agent travel           | Deploying time `0,.225,.45,.675,.899,.9 s`; then hide document for `.3 s` at `.45`                                    | progress `0,.25,.5,.75,.998888...,null`; hidden interval freezes `.5`; power still begins exactly at `.9`                                                                    |
| Installed retention    | Powered sites retained through next leg, crash, and two checkpoint restores                                           | each existing NOC-entry path stays installed; exact world count remains 76 and no can/power state duplicates                                                                 |
| Outcome/action rail    | Launch-ready, then failed                                                                                             | banner-only deployed state; crashed status plus Retry; Exit stays bottom-right in the active rail                                                                            |
| Interactive pointer    | `pointerdown` targets Retry descendant and Exit descendant, then native click                                         | Retry guard has no stage flight effect; Exit cannot reach stage; each native click runs exactly once                                                                         |
| Interactive keyboard   | Focus Exit/Retry; target each button or nested span with Space, Enter, arrows, `h`, and `l`                           | no flight prevention/held edge/queue/thrust; Space/Enter run one native action; arrows/`h`/`l` run no action                                                                 |
| Outside-shell keys     | Active mission; target header, breadcrumb, and descendants with Escape, `r`, and flight keys                          | no prevention, focus/state/action/input/model change; outside keyup is also inert after focusout clears input                                                                |
| Controls lines         | 320 px and 400%-equivalent layouts; keyboard child then touch child                                                   | one client rect per line; every relevant `scrollWidth<=clientWidth`; Exit in row 2; no authored-copy assertion                                                               |
| Refuel ratio           | Base `n=1,2,3,4`; test minimum `8`; carried fuel `7,20,5,4`                                                           | ratios `2,1.5,1.25,1.125`; awards `16,12,10,9`; reserves `23,32,15,13`; next ratios direct from `n+1`                                                                        |
| Ratio precision        | Direct Number formula at bases `52,53,54,100`                                                                         | `1.0000000000000004`, `1.0000000000000002`, `1`, `1`; never below `1`, no bound or arbitrary precision                                                                       |
| Safe inclusive edge    | Target top; `vx=2.2,vy=-3.6,angle=-18,omega=26`                                                                       | safe contact                                                                                                                                                                 |
| Unsafe epsilon         | Four contacts, each increasing exactly one boundary magnitude by `1e-9`                                               | each is unsafe; mirrored absolute-value signs and positive-`vy` rejection are independently covered                                                                          |
| Swept unsafe equality  | Hull only grazes terrain/truss/column/NOC/mast between step endpoints                                                 | repeated polynomial root reconstructs actual closed unexpanded contact; adjacent `.02 m` candidate samples remain clear                                                      |
| Target-top separation  | Safe descent over deck center; exact tangent; near miss; former depth/time cutoff vector                              | descent/tangent use isolated actual roots; near miss/cutoff enclosure is clear, never a synthetic crash                                                                      |
| Large procedural sweep | Finite low sweep across more than 64 old intervals and beyond both retained terrain edges                             | earliest global terrain contact; no retained-edge fall-through and no `overspeed` result                                                                                     |
| Large empty sweep      | Same X/angular travel wholly inside the rails with hull above ordinary candidate maxima                               | O(1) broad-phase clear; no vertex/knot allocation, work cap, or synthetic result                                                                                             |
| Unwrapped rotation     | Sweeps of `181`, `360`, and `721` degrees with a thin contact only during the positive unwrapped affine-knot sequence | exact unwrapped angular travel finds contact; shortest-arc mutation misses and fails                                                                                         |
| Maximum knot stream    | Exact maximum-reachable `50825.333333333336`-degree travel with a thin contact confined to the final affine slab      | streams exactly 50,828 knots in each direction, returns the same actual contact, retains no knot-count-sized array, and leaves DOM/listener/model lifecycle counts unchanged |
| Empty-fuel ballistic   | Exhaust fuel above the former `y=56` ceiling, coast, then return under gravity                                        | remains flying while clear, then crashes only at the first concrete swept contact                                                                                            |
| Frame equivalence      | Initial approach, no input, callbacks to 1,000 ms at 30, 60, and 120Hz                                                | 120 steps; `x=30.8`, `y=30.0875`, `vx=0.8`, `vy=-3.4`, fuel `15`                                                                                                             |
| First landing          | All eight S0-S7 opening profiles; section 10.2 off/on/off schedules; opening fuel `15`                                | safe contacts at steps `434..621`; at least `13.632` fuel remains                                                                                                            |
| Free exploration       | Cross `x=-5`, `101`, target right edge, former ceiling, then reverse inside the rails                                 | stays flying absent real contact; camera continuous; cue right/left/right; progress unchanged                                                                                |
| Sky parallax           | Same seed, camera left `0,50,-50`; five derived sky chunks                                                            | transform `0,-120,120 px`; 20 stars and 1-2 landmarks; two path nodes and exact regeneration on return                                                                       |
| Checkpoint replay      | Award, manual launch, crash, Retry twice                                                                              | exact deep checkpoint projection both times; no can, award, ratio, route, power, or progress duplication                                                                     |
| Initial Retry          | Crash before first powered base, then click Retry and later use `r`                                                   | exact same-seed initial pose/site/window/fuel/progress/ratio; shell focus; no synthesized input                                                                              |
| Allowance arithmetic   | All 243 proof records and independent engine exhaustion vectors                                                       | `B=12.65`; exact climb surcharge/quantum ceiling; partial final burn then zero thrust, without route-failure claim                                                           |
| Short-tap capture      | Down at `0`, eligible up at `20`; release synchronously emits lost capture                                            | token/deadline exist before release; pulse remains through `139.999`, ends once at `140`; later loss is no-op                                                                |
| Input overflow         | 65 alternating edges before one step at 30, 60, and 120 Hz                                                            | queue becomes one next-step physical-state snapshot; all frame schedules produce the same result                                                                             |
| Long run               | 100 successful deterministic sites                                                                                    | ratios are non-increasing and `>=1`; O(1) direct formula; bounded nodes/edges; exact reserve accounting                                                                      |

The eight first-landing records pin their last-clear and contact poses in derived v8 rather than
duplicating those literals here. Tests compare contact kind, step, pre-award reserve, and pose with
`1e-10` tolerance; none calls a production helper to build its expected schedule.

### 14.1 Phase 4S feasibility and pinned terrain vectors

Before this amendment was committed, disposable independent tooling froze section 5.2's 16/512 m
candidate. The exact eight rows reach `.10/.60`, actual maximum absolute grade `.32`, and maximum
adjacent-grade change `.40`. Each row has 6-8 reversals with 16-160 m spacing. Independent 4,096
vertex reconstructions for seeds `11`, `39`, `41`, and `STATIC_WORLD_SEED` contain all eight
profiles, zero adjacent profile repeats, no exact period through 64 vertices, and absolute Pearson
autocorrelation below `.22` at every lag 16 through 64. This is the pinned nonperiodicity/variety
witness; implementation tests recompute it from declared inputs instead of comparing authored
descriptions or screenshots.

Disposable Chromium rendered strong-band and higher-reversal ordinary windows for those seeds at
`1000 by 780`, plus exact `320 by 780`, `320 by 240`, and touch `667 by 320` views. Direct
inspection found strict angular/miter facets, broad valleys and recoveries with smaller asymmetric
changes, no 128 m reset or forced high/low rhythm, locally derived decks, and all six integrated
lattice rails terminating independently at native terrain. CDP measurements at all four viewport
sizes report `innerWidth=clientWidth=scrollWidth`, `innerHeight=clientHeight=scrollHeight`,
`scrollX=scrollY=0`, and computed stage aspect ratio `25/16`. Screenshots are qualitative review
artifacts only; no golden image or authored-prose assertion ships.

Independent arithmetic pins `N=(640-sceneY)/640=(92+10*worldY)/640` and `worldY=64*N-9.2`. For
`C_i=36+96*i`, exhaustive profile closure produces exactly 16 spatial phases, 320 assignments, 243
exact endpoint pairs, 224 deltas, decks `[-.268,31.7]`, and deltas `[-19.744,17.704]`. The bounded
solver exactly replays all 243 under unchanged controls, physics, fuel, landing limits, and concrete
collision: 208 use 33 independently certified schedules and 35 residuals are synthesized by the
pinned recipe. All contact by step 4,247 with burn at most `12.81125000000022`, hull top at most
`53.4231353132934`, and base burn at most `12.604750000001879`, preserving `B=12.65`. The branch
census is 41 deep falls, 78 shallow falls, and 124 flat/rises. All eight opening profiles pass from
the unchanged initial pose and fuel 15 with `originSiteId=null`. This proves design feasibility;
implementation must independently generate v8 fixtures and fresh real-browser evidence from shipped
code.

The artifact-review correction adds no obstacle to those routes: `.02 m` expansion is detection
only, and section 9's affine slabs are the sole canonical within-step sweep rather than an
approximation to a second curved pose authority. Across the 243 stored contacts, maximum absolute
landing values are `vx=1.1551981660881976`, `vy=2.637967138882803`, `angle=15.754404597863527`, and
`angularVelocity=24.372795201853602`, leaving respective inclusive limit margins
`1.0448018339118026`, `.9620328611171969`, `2.2455954021364732`, and `1.6272047981463977`.
Independent bound arithmetic gives total fuel `<76237.8`, maximum one-step translation
`5717.8566666666675 m`, candidate depth `19`, maximum 50,828 knots, and `235395422082<2^38` for the
deliberately loose altitude bound. These figures establish feasibility of the bounded classifier and
comfortable recorded landing envelopes; permanent v9 replay remains the authority that all 243
corrected collision records and eight openings still pass before any fixture is accepted.

## 15. Verification matrix

The exact local demo remains
`python3 website/build.py --repo-root . --output /tmp/agentworks-site-demo --site-base /`, followed
by `python3 -m http.server --directory /tmp/agentworks-site-demo 8000`. Game work opens `/lander/`;
fallback acceptance opens `/404.html`.

The complete artifact contains exactly these 13 files:

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
static/onboarding-copy.js
```

Final ordinary generation and independent verification pin newly generated v8 geometry, physics,
proof, world, and output digests atomically. All 243 exact pair records, eight opening records, and
the one terminal record must be present; old pre-correction digest literals and
six-template/smaller-failure data are absent, not aliases.

| Layer                                                                   | Required coverage                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `node --test website/tests/lander-world.test.mjs`                       | Independently reconstruct the eight global profiles from seed and signed superblock/epoch only, strict 16 m X cadence, exact normalized mapping/band, linear interpolation, terrain miter join/limit `2`, exact `.36/.40` grade/change bounds, sixteen 96/512 m phases, all 320 assignments and 243 exact pairs, closed-footprint maximum, `+2.5 m` deck equality, and signed regeneration. Pin the exact closed world/site bounds, terrain terminal indexes, two `.2 m` native-foot rails, clamped edge projection, and one permanent terminus path distinct from chunks. Across 4,096 vertices retain the variety witnesses. Rebuild 14 truss members, three uncapped finite lattice columns with six native feet and exact colliders. Mutation-kill forced alternation, site-conditioned terrain, phase lock, curves, rounding, a global datum, shelf/cap, retained-edge collider, missing/moved terminus, vertical terrain edge, or foot drift.                                                                                                                                                                                                                                                                      |
| `node --test website/tests/lander-route-proofs.test.mjs`                | Reproduce the generated proof source byte-for-byte from reviewed derived v8; replay all 243 success records over 320 assignments, one keyed lookup per pair, contact `<=4320`, and exact complete collision with no hull-height rejection. Pin `B=12.65`, exact no-epsilon quantum ceiling, sufficient allowances, atomic corrected digests, S0-S7 openings, terminal record, signed longevity, and absence of `smallerFailure`/runtime search.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `node --test website/tests/lander-model.test.mjs`                       | Retain unchanged inclusive `2.2/3.6/18/26` landing limits, engine exhaustion arithmetic, fuel, ratio, checkpoint, Retry, zero-fuel, deployment, reduced-motion, hidden-time, collision integration, input, and scheduler vectors without growing this near-ceiling module. No assertion encodes authored prose.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `node --test website/tests/lander-phase4s.test.mjs`                     | Mutation-sensitive independent reconstruction of the `.02 m` detection-only candidate invariant and unexpanded linear/quadratic contact equations. Kill expansion-as-impact, endpoint-only contact, discriminant-sign rounding, repeated-root loss, collinear-overlap loss, a Number midpoint that stops progressing, shortest-arc knots, retained knot arrays, work-budget impact, retained-edge impact, invisible/moved rails, final-site route lookup, terminal award/cursor/null-target drift, or a 14th artifact. Exercise both signed maximum-reachable streams, assert exactly 50,828 visited knots and final-slab actual contact, and preserve stable model/listener/DOM counts through repeated Retry/lifecycle calls. Keep this focused test and every authored module below 1,000 lines.                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `node --test website/tests/lander-phase4l.test.mjs`                     | Mutation-sensitive controller/DOM tests pin exactly two controls-line children in keyboard/touch order, Retry label-source and hint structure without asserting text, internal `RESTART` dispatch, crash focus stability, click/`r` teardown-render-focus order, shell focus with `preventScroll`, and no synthesized input. Existing outside-shell and native-button rejection coverage remains exact.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `node --test website/tests/lander-phase4m.test.mjs`                     | Mutation-sensitive controller/DOM tests pin five sky chunks, 20 stars, one or two deterministic landmarks in exactly two paths, the complete two-arc crescent, all three one/two-ring profiles, exact circle/ellipse intersections, omitted rear-center arcs, and complete foreground arcs. They retain static/dynamic descriptor equality, bounded reconciliation, `.24` parallax transforms, negative/positive camera following, bidirectional cue changes, pass/reverse/return, and the historical rejection of a generic horizontal-bound failure; Phase 4S's focused test owns the exact two-rail contact and camera-clamp exception. They also pin the exact opening half-gauge, post-award full reference without cap, `.9 s` deploy travel, unchanged refuel/power timing, hidden-time freeze, reduced-motion atomic projection, and structural copy/link/accessibility sources without embedding authored wording.                                                                                                                                                                                                                                                                                              |
| Derivation CLI fixture verification                                     | Verify the immutable v7 bootstrap byte hash/schema/count/internal digests, extract its narrow schedule projection, and generate temporary v8 output with v9 deriver, collision v2, and synthesizer v1; review the canonical delta; update bootstrap/geometry/physics/assignment/proof/world/output digests atomically; then run ordinary `--verify`. Enumerate exact 16-phase/profile closure into 320 assignments and 243 numeric-sorted exact pair keys. Regenerate with pinned bootstrap order/selection, prefix, `12/269/6000` bounds, command order, integer-millimeter key, cost/tie tuple, first-safe-layer selection, and 4,320-step ceiling; exact independent and production replays agree for all 243 routes, eight openings, and terminal vector. No early-success omission, profile rejection, runtime fallback, fuel-wasting schedule, scope/order drift, undeclared input, or partial fixture is permitted.                                                                                                                                                                                                                                                                                               |
| `python -m unittest discover -s website/tests -p 'test_*.py'`           | The validator pins one shared fragment, structure, accessible-name sources, v8 fixture/schema names, and absence of old v7 production authority, Phase 4P, smootherstep, curve, global-datum, site-conditioned terrain, vertical-camera, ceiling, and overspeed fields. Exact artifact, DOM budget, privacy, module DAG, route uniqueness, recovery, and static/dynamic parity remain. It does not assert authored prose.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Automated Chromium projection witness                                   | At exact `1000 by 780`, `320 by 780`, real `320 by 240`, and touch `667 by 320`, first assert requested inner dimensions and exact client/scroll equality with zero scroll. Every action/landmark stays inside the viewport through wheel/touch/focus/game transitions. Keep the `25/16` stage across preflight, low valley, summit, clear flight above former ceiling, empty-fuel ballistic return, concrete crash, service, Retry, reversal, and both edge approaches, with world Y transform zero and no vertical-camera state. Capture seed `11`, `41`, static broad/jagged windows, each visible terminus before contact, actual rail-contact crash, and the final-site null cue. After one unmeasured production-identical warmup per direction, run ten maximum-knot classifications in each signed direction. Every run visits exactly 50,828 streamed knots; nearest-rank p95 over the 20 runs stays below 100 ms and maximum below 250 ms on the pre-merge Chromium machine; instrumented knot-hull retention stays at most two and the slab stack at most 20; DOM/listener counts equal their baseline after Retry. Retain focus, cue, parallax, gauge, deployment, reduced-motion, and checkpoint witnesses. |
| Pseudo-can computed-style and screenshot witness                        | For `getComputedStyle(stage,"::after")`, assert `width=20px`, `height=22px`, `pointer-events=none`, `image-rendering=pixelated`, transparent background color, exactly six gradient images, sizes `6px 2px,10px 6px,2px 4px,4px 8px,12px 14px,16px 18px`, positions `6px 2px,4px 0px,16px 10px,16px 8px,2px 6px,0px 4px`, `no-repeat` six times, and alternating normalized paints `rgb(217,74,30)`/`rgb(41,43,48)` in the pinned top-to-bottom order. At DPR 1 and an integer transfer center, take exact `20 by 22` CSS-pixel crops with refueling on and off: on-crop probes `(5,1)`, `(7,3)`, `(18,9)`, `(16,11)`, `(1,5)`, and `(3,7)` prove the six graphite/orange parts; `(0,0)` and `(19,21)` are byte-equal to the off-crop background, proving transparency. The crop visibly reads as one block can. No golden asset ships.                                                                                                                                                                                                                                                                                                                                                                                  |
| Human-authored copy review                                              | A reviewer compares the document title, headings, 404 explanation, controls, outcome/status, action labels, and visible shortcut hints with sections 4 and 13. This is deliberately human evidence; automated suites do not encode authored phrases, substrings, or blacklists.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| Manual Chrome and Edge pre-merge; Firefox and Safari/WebKit post-launch | Confirm ordinary windows visibly show only angular straight segments with broad navigable peaks, valleys, rises, and descents using substantial lower/upper band. Reject any smooth/curved impression, two-level sawtooth, noisy grade changes, or terrain shaped around sites. Each deck is exactly `2.5 m` above its closed-footprint maximum, with three integrated columns independently meeting native terrain however long. Confirm fixed scene height, no vertical follow/page growth/scroll, backing face, seam, floating foot, or vertical artifact. Retain every non-terrain behavior.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Responsive, zoom, focus, and accessibility acceptance                   | At exact `320 by 780`, real 400-percent-equivalent `320 by 240`, touch-landscape `667 by 320`, and `1000 by 780`: stage and rail remain normal-flow separated; gauge/outcome, crash/Retry, controls lines, and Exit do not overlap or overflow; buttons are at least `44 by 44`. A real accessibility-tree witness derives expected names from current visible label nodes, proves hint exclusion and shortcut ARIA, one live region, controls IDREF, and tab order shell then Exit or shell then Retry then Exit. Retry returns to shell, Exit to Start; no trap. Authored strings are reviewed by a human, not embedded in automation.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| Performance and longevity witness                                       | For each seed `11`, `39`, `41`, and `STATIC_WORLD_SEED`, generate and power 100 sites in both directions with one exact proof-record lookup per ordinary site, no retry/generation error, all sixteen spatial phases, and all eligible profiles. Keep bounded retained sites/chunks/fragments/actions/sky and no retained history; collision queries procedural in-domain terrain outside retained projection. Include large low/high sweeps, >180-degree rotation, repeated-root tangency, empty-fuel ballistic return, and separate final-site/terminus lifecycle without a route lookup. Each site owns one finite uncapped scaffold path. Record generation, derivation, proof, collision, root-isolation, and frame timings against existing ceilings.                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Permanent documentation and repository gates                            | `website/README.md` and browser checklist teach the changed actions, ordinary departure, tolerances, rail, accessibility, shared-fragment/no-JS behavior, and derivation workflow in lockstep. Focused suites, deterministic root/project builds, complete gates, file lint, locked-SDD, Rulesync drift, module-size report, and an exact intended-file diff pass. Permanent docs do not link to this SDD.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |

Automated and reviewer acceptance cannot close Phase 4S. After every required check above is green,
the operator must personally play representative seed `11`, seed `41`, and one fresh random run at
wide and exact 320 CSS layouts. The hands-on gate explicitly accepts or rejects: visibly straight
angular terrain; substantial lower/upper band use; broad peaks, valleys, and intermediate facets;
non-sawtooth grade changes; each deck's local height and integrated support scale; fixed scene
height; no vertical follow, page growth, or scroll; and unchanged landing/control feel. Until the
operator records affirmative acceptance, status stays pending and Phase 4S must not merge. A
rejection returns to design; implementation must not answer it by weakening collision, clearance,
landing, route, or physics bounds.

The 100-site witness asserts each seed's exact indexed superblock profiles and all sixteen spatial
phases, then proves `C_i=36+96*i`, global terrain independence from site state, and byte-identical
signed regeneration after eviction. It establishes bounded direct construction rather than
fixture-only replay without requiring deck/profile repetition.

Mutation tests reject duplicated/moved shared markup, a second scheduler/controller/site authority,
game checks added to the near-limit validator, artifact count drift, a sixth retained chunk, more or
fewer than the exact fill/surface terrain pair, a stroked fill, closed surface, surface floor point,
internal closure edge, concatenated per-chunk path, non-increasing X, same-X/different-Y pair, a
terrain join other than `miter`, terrain miter limit other than `2`, or chunk/site boundary
mismatch. They reject seed/global-superblock/epoch/profile/signed-index drift, selector dependence
on a site or prior call, an epoch missing or duplicating a profile, adjacent profile equality, a
cadence other than 16 m, superblock width other than 512 m, an internal 128 m `.35` reset, an exact
period through 64 vertices, autocorrelation outside section 5.2, any nonlinear or rounded terrain
interpolation, an anchor outside `[.1,.6]`, grade above `.36`, adjacent-grade change above `.40`, a
forced H/L rhythm or two-level sawtooth, any shelf/cap/blend/discard splice, terrain access during
site-X selection, a column rail foot not equal to native interpolation, closed-footprint maximum
drift, a deck not exactly `2.5 m` above that maximum, a global datum, or any candidate/terrain retry
or profile rejection.

Collision mutations reject procedural terrain sampling that stops at a retained render edge, any
synthetic future/evicted structure descriptor, a finite travel/interval/speed/altitude failure,
`MAX_PLAYABLE_Y`, an absent/invisible/moved terminus, a retained-edge collider, a cutoff-derived
contact, shortest-arc interpolation, right-first traversal, or an enclosure/expanded sample treated
as impact. Exact vectors cover high O(1) pruning; a low sweep crossing more than 64 former intervals
and both render edges; actual crossing, repeated-root stationary tangency, collinear overlap, and
near miss; retained active/target/preceding structure precedence; both physical terminus contacts;
empty-fuel ascent above and return below the former ceiling; and physical angular travel beyond 180
and 360 degrees. They require earliest reconstructed unexpanded contact and unchanged exact-time
feature precedence.

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
the wrong first diagonal; or any maximum support length/member count that truncates the exact
terrain-derived lattice. They reject a regional perimeter, post grid, X brace, any surviving
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
transform/bounds/paint drift, failure to restore the absent doorway, a second NOC-entry or child, an
absent or duplicate permanent terminus path, any other added world descendant, a maximum other than
exact 76 or greater than 80, and any unexpected route/physics/geometry/world/output digest change.

The historical Phase 4M/4O projection witness continues to reject its retired generic horizontal
world-edge failure/clamp and one-direction camera. Phase 4S supersedes only that unbounded-X clause:
section 5.4's exact two physical rails and the section 6 camera clamp at those rails are required.
Mutations reject zero, one, or three rails; any rail position, width, visibility, or collider drift;
contact at a retained terrain/site/camera edge; a camera clamp anywhere except the two rails; a
camera that cannot follow both signs of X inside those rails; any camera dead zone other than
section 6's exact two-sided formula; a cue that cannot produce `right/left/right` while passing and
returning; cue visibility after any part of the target enters the viewport; or a mirrored cue with
different geometry. They reject a sky speed other than `.24` of camera motion, a sky window other
than five `50 m` chunks, a count other than four stars per chunk, a landmark cadence other than
every fourth chunk, or a landmark other than the seeded crescent/planet choice. Planet mutations
reject a profile other than exact radii `[(28,9)]`, `[(31,10)]`, or `[(28,9),(34,12)]`; a ring count
outside one or two; circle-intersection drift; a visible rear-center arc; a missing foreground arc;
quadratic geometry; or the old full ellipse. Sky mutations still reject a group/path count other
than `1/2`, retained off-window sky history, nondeterministic regeneration, or any sky semantics,
collision, network, or storage state. Battery/signal mutations reject a mast or antenna head that
changes from fixed `#292b30` at any power stage while retaining the three established arch colors
and timings. They reject terrain projection beyond the visible buffered interval, a count above the
independently derived v8 ceiling, failure to reproduce the exact strict-X 16 m lattice plus required
boundary/structure insertions, or retained offscreen sites expanding that projection. They also
reject agent travel other than `.9 s`, any change to `.3 s` refuel or `1.4 s` power, hidden-time
advancement, or reduced motion that exposes an intermediate stage.

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
iterative refuel-ratio advancement, trusting a ratio inconsistent with
`refuelRatioForBase(completedSites+1)`, runtime planning/search/fuel scan, more than one keyed
lookup or defensive replay, an unreachable command, production-derived fixtures, geometry/derived
schemas other than v8, deriver other than v9, collision recipe other than v2, synthesizer other than
v1, assignment/pair/delta counts other than `320/243/224`, a contact after step 4,320, missing any
of 243 success, eight opening replays, or the terminal record, any `smallerFailure`,
fuel-wasting/loitering schedule, base allowance other than 12.65, terminal delta other than zero,
terminal route lookup, descriptor `4096`, descent credit, partial route/world regeneration, witness
seeds other than `[11,39,41,STATIC_WORLD_SEED]`, wrong witness nesting, derivation-tool imports, or
reviewed digest drift. Derivation mutations additionally reject bootstrap byte/schema/digest/count
drift, reading bootstrap authority beyond its pinned narrow fields, failure to recompute a schedule
digest, run deduplication/order/source-pair/first-safe-selection drift, or any prefix
run/count/state drift; phase, `12/269/6000`, candidate, expansion, or replay ceiling drift;
command-order, `Math.round` key, waypoint, cost, comparison-tuple, first-safe-layer, or run-merging
drift; a non-numeric pair-key sort; assignment loop/ID/member-order drift; a world-witness
field/order drift; or any digest payload that adds, removes, rounds, or reorders authority outside
its pinned scope. Closed unsafe collision, unexpanded target-top handling, transactional
initialization, fixed retention, reversible camera motion, normal crash debris, ballistic fragments,
non-animated direction meaning, vacuum presentation, lifecycle cleanup, privacy, and
zero-runtime-network constraints remain mutation-protected.

Phase 4S projection mutations additionally reject any world Y translation, `--camera-y`, vertical
camera state/helper/import, pose-dependent viewBox or stage size, stage min-height growth, terrain-
or-flight-dependent page block growth, non-clipped stage overflow, rail overlay, any document
`scrollHeight!=clientHeight` or nonzero `scrollTop`, or any required viewport where document or
component `scrollWidth>clientWidth`. Screenshot review rejects an ordinary site window that lacks
visibly straight angular facets or substantial lower/upper-band relief, reads as curved, flat,
noisy, or site-shaped, or whose support fails to terminate independently at native terrain.

Refuel-ratio mutations independently reject a first powered-base number other than one, a formula
other than `1+0.5^(n-1)`, the former `3` start, the former `.82` recurrence or epsilon floor,
iteration over prior bases, using the post-increment ordinal for the current award, replacing rather
than adding to carried fuel, rounding/capping the award or reserve, advancing more than once,
recomputing checkpoint authority from post-crash state, arbitrary precision, an artificial
base-count bound, a runtime value below `1`, failure to round to exact `1` at `n=54`, or any effect
on route proof, sufficient allowance, or proof digests. Exact mutation vectors pin `n=52,53,54,100`
and prove the 100-site sequence is non-increasing and never below `1`; they do not require it to
remain strictly above `1`.

## 16. Traceability

| Requirement or decision                                                                 | Pinned by                                           |
| --------------------------------------------------------------------------------------- | --------------------------------------------------- |
| R6, D5: selected custom mark, twin plumes, and favicon                                  | Sections 2 and 3                                    |
| R7, AC5, AC19: hidden shared 404/Lander game and byte-equivalent DOM                    | Sections 4, 11, and 15                              |
| R8, AC6/AC24: near-half steering, keyboard/vi/touch, independent plumes                 | Sections 8, 11, 12, 14, and 15                      |
| R9, AC8: no-JS, in-memory lifecycle, pause, focus, reduced motion                       | Sections 4, 5.1, 8.2, 11, 12, and 15                |
| R18: exact Lander title/`h1` and 404 explanatory copy                                   | Sections 4 and 15                                   |
| R21, AC7: gauge, payoff, manual departure, battery/signal, legs                         | Sections 4, 7, 10.3, 12, 14, and 15                 |
| R22, AC22: seeded target, sufficient allowance, ratio, carryover                        | Sections 5, 7.3, 10, 14, and 15                     |
| R21/AC22: elevated open scaffold with honest conservative colliders                     | Sections 5.3, 9, 10.2, 14, and 15                   |
| R22, AC23: offscreen target and motion-safe bidirectional cue                           | Sections 6, 8.2, 12, and 15                         |
| R23, AC24: vacuum crash and exact checkpoint Retry                                      | Sections 7.3, 9, 13-15                              |
| R24, AC25: arcade gauge/transfer, outcome/Retry, rail, installed agent                  | Sections 4, 6, 7, and 11-15                         |
| AC18: complete build only and exact local manifest                                      | Sections 2 and 15                                   |
| Phase 4G: focused modules, bounded work, docs, and browser evidence                     | Sections 2, 6, 14, and 15                           |
| Phase 4H: terrain, support, control, landing, and NOC tuning                            | Sections 4-6, 8-10, 12, 14, and 15                  |
| Phase 4I: gauge, banner, accessible departure, force, structure, copy                   | Sections 4-12, 14, and 15                           |
| Phase 4J: arcade chrome, refuel projection, and installed-agent payoff                  | Sections 4, 6, 7, and 11-15                         |
| Phase 4K: action rail, manual departure, and safe-contact envelope                      | Sections 4, 6, 8-12, 14, and 15                     |
| Phase 4L: Retry, refuel ratio, relaxed landing, and continuous truss                    | Sections 1, 4, 5, 7, 9, 10, 14, 15                  |
| Phase 4M: lattice, honest fuel, parallax sky, and free exploration                      | Sections 1, 4-10, 12, and 14-15                     |
| Phase 4Q: fixed-height relief, local decks, and terminating routes                      | Sections 5, 6, 10, 14.1, and 15                     |
| R27, AC28: Phase 4P rollback, fixed scene, local decks, and browser/operator acceptance | Supersession record and Sections 5, 6, 14.1, and 15 |
| R28, AC29: global straight terrain, independent sites, exact local decks, fixed scene   | Sections 5, 6, 10, 14.1, and 15                     |
| R29, AC30, Phase 4S: less-periodic terrain and contact-only free flight                 | Sections 5.2, 5.4, 6, 7.2, 9, 14.1, and 15          |

Implementation treats this LLD as temporary design input. Permanent source, tests, and
`website/README.md` stand on their own and do not link back to this SDD path.
