# LLD: AGW Brand and Continuous Lunar Deployment Lander

<!-- cspell:ignore arcade Cascadia Consolas focusout IDREF IDREFs imul keyup Menlo -->
<!-- cspell:ignore pointerdown pointerup PRNG -->
<!-- cspell:ignore letterboxing parallax refuel reproject reprojection reprojects repower Segoe -->
<!-- cspell:ignore affinely autocorrelation halvings lerp Minkowski nonperiodicity overspeed -->
<!-- cspell:ignore significands subinterval superblocks unhashed unmarginated -->
<!-- cspell:ignore substep transactionally underframe unitless uint32 -->
<!-- cspell:ignore quantized quantization Warren -->
<!-- cspell:ignore smootherstep viewports -->

- Status: Phase 4U simplified route design complete; implementation blocked on independent review
- Operator browser acceptance: pending
- Date: 2026-08-15
- FRD: `frd.md`, specifically R6-R9, R15-R25, R28-R31, AC26, and AC29-AC32
- HLA: `hla.md`, specifically D5 and D7
- Plan: `plan.md`, specifically Phase 4U
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
terrain-derived deck/support geometry, and the route evidence that necessarily consumes that
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

Phase 4T preserves Phase 4S's global selector, fixed scene, collision, finite world, site geometry,
native supports, service/checkpoint semantics, and honest sufficient-allowance formula. It
supersedes only the eight terrain-profile rows, the route-only site-center proposal needed to keep
accepted decks at or below normalized `.5`, and the terrain/route fixture bytes derived from those
changes. No control, vehicle physics, landing limit, fuel consumption, refuel-ratio formula, service
timing, sky, copy, power-up, collision outcome, finite-world behavior, or page-layout contract
changes.

Phase 4U preserves Phase 4T's selector, reversal density, candidate eligibility, local structure,
native supports, collision authority, finite-world contact semantics, mission lifecycle, controls,
fuel flow, landing limits, refuel-ratio formula, fixed scene, and no-scroll projection. It
supersedes only the eight profile rows, nominal site stride and derived physical-rail positions,
translational mass/acceleration constants, constant-time allowance policy, and the fixture bytes
that necessarily consume those authorities. Angular acceleration remains exactly `80`; no rigid-body
inertia change, fuel-mass coupling, camera change, or unrelated presentation change is introduced.

## 1. Scope and terms

This LLD preserves the selected brand and defines the continuous Lander and arcade presentation in
R7-R9 and R21-R25, including the Phase 4M free-exploration, lattice-column, parallax-sky,
half-reference opening, faster-deployment refinement, and Phase 4U straight-polyline terrain. It
excludes main-page, onboarding, deployment, and DNS design. Use plain HTML, CSS, SVG, and
JavaScript.

A **run** begins at START and ends at Exit or reload. A run contains successive **legs**, each from
one checkpoint or the initial approach to one target site. A **site** is one platform, gas can, and
NOC. **Commanded thrust** is the post-input, post-assist, post-fuel engine value shared by physics
and plumes. **Manual steer** is the normalized signed turn intent before angular assistance;
negative is left and positive is right. **Thrust-vector angle** is the manual-steer-derived
direction shared by both engine forces and both rendered plumes while effective thrust is nonzero;
it is zero at zero effective thrust. **Mission time** excludes hidden time. A **sufficient
allowance** is section 10.1's conservative base plus the monotone positive deck-delta surcharge,
rounded up to the pinned fuel quantum. It is neither a schedule-specific minimum nor a universal
flight guarantee or global mathematical optimum over all possible controls. The **refuel ratio** for
the one-indexed successfully powered base number `n` is exactly `ratio(n)=1+0.5^(n-1)`. The first
successfully landed and powered base is `n=1`. That mathematical sequence approaches `1` from above;
section 10.3 pins its exact JavaScript Number projection, including the finite-precision value `1`
from `n=54` onward. The **fuel-gauge reference** is the positive model-owned denominator used only
by the visual gauge. It starts at `30` against the opening reserve of `15`, then becomes the exact
uncapped post-award reserve whenever an award establishes a later leg. It is neither a tank capacity
nor necessarily the amount present when an initial leg starts. A **refuel projection** is the
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

| File                                                    | Responsibility                                                  |
| ------------------------------------------------------- | --------------------------------------------------------------- |
| `website/assets/agw-rocket.svg`                         | Canonical selected A, G, W, and twin-plume geometry             |
| `website/assets/agw-favicon.svg`                        | Flame-free browser projection of the selected A/G/W mark        |
| `website/templates/404.html`                            | Semantic 404 shell with the shared game placeholder             |
| `website/templates/lander.html`                         | Dedicated Lander shell with the shared game placeholder         |
| `website/templates/lander-game.html`                    | Sole source for the complete reusable game subtree              |
| `website/build.py`                                      | Rendering and exact linked-artifact validation                  |
| `website/site_game_validation.py`                       | Focused game DOM, module-closure, and manifest validation       |
| `website/static/lander.css`                             | Scene layout, state selectors, focus, and SVG presentation      |
| `website/static/lander-collision.js`                    | Pure exact continuous-contact arithmetic                        |
| `website/static/lander-world.js`                        | Pure seed, terrain, site, collision, and window functions       |
| `website/static/lander-model.js`                        | Authored flight/run state, physics, and direct allowance        |
| `website/static/lander-game.js`                         | DOM, clock/input, camera, focus, lifecycle, and rendering       |
| `website/static/onboarding-copy.js`                     | Existing canonical onboarding copy; preserved without changes   |
| `website/tools/derive_lander_geometry.mjs`              | Independent Phase 4U geometry-fixture generation CLI            |
| `website/tools/lander_clear_faces.mjs`                  | Independent scaffold-overlay and clear-face enumeration         |
| `website/tests/fixtures/lander-route-geometry-v10.json` | Canonical Phase 4U terrain, site, world, collision, and physics |
| `website/tests/lander-world.test.mjs`                   | Seeded world, window, site, and geometry vectors                |
| `website/tests/lander-model.test.mjs`                   | Scheduler, physics, mission, fuel, and checkpoint vectors       |
| `website/tests/lander-phase4s.test.mjs`                 | Finite-world, continuous-contact, and terminal-site vectors     |
| `website/tests/lander-phase4t.test.mjs`                 | Sharper terrain, bounded candidates, and fixture vectors        |
| `website/tests/lander-phase4u.test.mjs`                 | Final terrain, pacing, mass, finite-world, and fixture vectors  |
| `website/tests/test_lander_404.py`                      | Build, DOM, no-JS, and forbidden-surface checks                 |
| `website/tests/lander-browser-checklist.md`             | Package-free browser, performance, and accessibility acceptance |

The complete shipped artifact contains both `static/lander-world.js` and the focused
`static/lander-collision.js`; tool, test, and fixture files are not artifact entries. `build.py`
copies all four production modules directly without source composition. The output remains the exact
14-file artifact. The shipped production module DAG is exact:

```text
lander-game.js  -> lander-model.js -> lander-world.js -> lander-collision.js
       |--------------------------------^  read-only projection and seed helpers
```

`lander-game.js` imports the model API plus only the pure `cameraLeftForPose`, `CHUNK_WIDTH`,
`mixUint32`, `siteScaffoldPath`, `siteStructure`, `skyProjectionForCamera`,
`skyProjectionIdentityForCamera`, `targetDirectionForViewport`, `terrainFillPath`,
`terrainSurfacePath`, and `terrainVerticesForRange` exports directly from `lander-world.js`. The
authored `lander-model.js` imports pure world construction, retention, seed, collision, and geometry
exports. `lander-world.js` imports only exact root comparison, reconstruction, and segment-contact
helpers from `lander-collision.js`. The collision module imports no production module; both lower
modules read no DOM, clock, storage, or ambient randomness and own no mutable singleton. No
production module imports upward or sideways outside this DAG.

The model is the sole mutable run authority. One run aggregate owns physics, fuel, mission state,
seed, generator cursor, retained sites, active and target IDs, checkpoint, and crash debris. The
controller is the sole browser adapter and owns browser listeners, the animation frame, focus,
pointer capture, CSS projection, and entropy acquisition. Neither lower module accesses a browser
global. The controller must not keep a second site, fuel, checkpoint, or mission copy.

Prefer each focused production, tool, or test module at or below 500 lines; every authored source
must remain below 1,000. Generated fixture bytes are not authored source, but their derivation,
schema, provenance, and review remain mandatory; authored logic may not be minified or
formatting-ignored to evade the ceiling. `website/site_validation.py` is already near the hard
ceiling. It imports `validate_game_contract` from `site_game_validation.py` and passes the rendered
pages, asset manifest, and site base into that focused authority. The helper imports only the
standard library, never imports `site_validation.py`, and owns all new game-subtree, game-module
closure, and exact game-manifest rules. `test_lander_404.py` exercises the helper. The helper is
build source, not a shipped site file, so the browser artifact remains exactly 14 files. Splitting
follows authority, not line compression.

Phase 4S adds exactly one focused shipped production module, `lander-collision.js`, and one artifact
entry. Move section 9's exact dyadic root comparison and linear/quadratic segment-contact arithmetic
into that pure module; retain candidate construction, feature precedence, and outcome classification
in `lander-world.js`, and do not retain or append a second classifier. Both shipped modules must be
below 1,000 lines before and after build; build composition may not hide an oversized module. Keep
the existing near-ceiling model test below 1,000 by placing new collision/terminus/terminal-service
coverage in the focused `lander-phase4s.test.mjs`; do not move unrelated assertions merely to game
the ceiling. The ordinary broad-phase-clear step remains O(1), an ordinary near-ground step reaches
approximately 10-12 candidate halvings and one or two angle slabs, and only pathological
maximum-reachable rotation can reach the declared 73,094-knot bound. That bound may cost work but
cannot become a failure.

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
the panel visually rather than hiding it. `data-banner="deployed"` and `"crashed"` project only
launch-ready and failed respectively. Only `failed` reveals and enables Retry beneath the crash
status. Exit remains visible and enabled at the bottom-right of the controls rail for every active
state. The resulting game-subtree tab order is shell, Exit in every active non-failed state and
shell, Retry, Exit while failed. The adjacent ordinary `span#lander-fuel-label` and
`span#lander-fuel-value` are the only accessible fuel-description sources. Both use the established
`.visually-hidden` class and neither has `aria-labelledby`, `aria-describedby`, `aria-label`,
`role`, `aria-live`, or implicit live semantics. The shell's ordered `aria-describedby` IDREFs name
the label and then the value separately, so the computed accessible description contributes both the
label text and changing numeric text instead of substituting the label for the value. The adjacent
gauge and fill are `aria-hidden` and cannot become a second meter, progress element, live region, or
named control. The controller sets only the value span's `textContent` to `fuel.toFixed(1)` when
that displayed tenth changes. Model fuel remains the exact unrounded number; this one-decimal
accessible presentation is intentionally rounded and is not an exact decimal encoding. Fuel is never
announced through status.

The controller owns this exact projection; `shown` means the element and every ancestor are not
hidden, and `enabled` applies only to native buttons:

| State projection                                      | Start            | Outcome / banner | Retry            | Controls rail / Exit  |
| ----------------------------------------------------- | ---------------- | ---------------- | ---------------- | --------------------- |
| Source no-JS or failed initialization                 | hidden, disabled | hidden           | hidden, disabled | hidden; Exit disabled |
| Successfully initialized `preflight`                  | shown, enabled   | hidden           | hidden, disabled | hidden; Exit disabled |
| `flying`, service, started `launching`, or `crashing` | hidden, disabled | shown / `none`   | hidden, disabled | shown; Exit enabled   |
| launch-ready `launching`                              | hidden, disabled | shown / deployed | hidden, disabled | shown; Exit enabled   |
| `failed`                                              | hidden, disabled | shown / crashed  | shown, enabled   | shown; Exit enabled   |

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
divides by `2 ** 32`. Stream `5` remains debris and streams `6..12` remain sky. Terrain streams
`13..14` remain retired; stream `15` selects one run-wide profile offset, stream `16` shuffles each
signed terrain epoch, and Phase 4U stream `17` selects the one run-wide terrain-independent
site-candidate order. Streams `1..4` and `13..14` remain unused. Indexed samples make regeneration
independent of call order. Neither terrain stream consumes a site index. There is no mutable PRNG
cursor inside the world module.

The checked-in preflight scene uses `STATIC_WORLD_SEED=0x41475731`. Production START requests one
`Uint32Array(2)` from `crypto.getRandomValues` and passes both words through
`mixUint32(word0 ^ rotateLeft(word1,13))`. If Web Crypto is unavailable, the controller mixes
integer `Date.now()` and the integer microsecond portion of `performance.now()` once. It never uses
`Math.random`. The resulting nonzero seed replaces the static scene and exists only in the run
aggregate. Tests call `createRun({seed})`; ordinary START, Exit followed by START, and reload
acquire a fresh seed. Retry reuses the current run seed and checkpoint.

### 5.2 Phase 4U global straight-polyline terrain

`CHUNK_WIDTH=50 m` remains retention bookkeeping; chunks do not generate terrain. Terrain is one
global lattice derived only from the normalized run seed and a signed global block/vertex index. It
does not read a site index, site center, footprint, deck, flight evidence, retained history, or
collision result. Sites likewise do not influence terrain phase or select among terrain profiles.

Canonical vertices occur every `16 m`. Groups of 32 segments form signed `512 m` superblocks. Each
superblock selects one of the following eight asymmetric 33-sample profiles. A profile returns to
`.35` only at its two 512 m boundaries; none of its internal 128 m cuts is `.35`:

```text
S0 .35 .29 .25 .10 .25 .30 .39 .47 .42 .41 .36 .28 .38 .45 .40 .30 .23 .28 .33 .26 .20 .30 .25 .19 .29 .35 .29 .22 .16 .10 .17 .26 .35
S1 .35 .45 .60 .45 .35 .34 .27 .36 .41 .49 .41 .36 .31 .26 .34 .43 .52 .47 .37 .43 .33 .29 .28 .23 .33 .40 .45 .38 .36 .28 .36 .44 .35
S2 .35 .30 .25 .35 .43 .36 .28 .33 .43 .33 .24 .29 .20 .19 .14 .23 .32 .37 .32 .38 .45 .60 .45 .42 .47 .52 .42 .36 .46 .39 .37 .28 .35
S3 .35 .45 .52 .46 .38 .43 .49 .54 .48 .38 .43 .49 .44 .39 .45 .60 .45 .36 .42 .50 .44 .36 .27 .35 .27 .18 .26 .36 .30 .25 .35 .44 .35
S4 .35 .26 .33 .42 .45 .60 .45 .41 .48 .39 .38 .29 .39 .29 .23 .31 .38 .28 .19 .28 .23 .18 .25 .32 .42 .37 .32 .40 .35 .28 .19 .28 .35
S5 .35 .45 .60 .45 .33 .39 .48 .38 .30 .28 .22 .27 .36 .42 .34 .31 .28 .20 .28 .37 .43 .51 .41 .33 .43 .48 .38 .31 .41 .43 .50 .43 .35
S6 .35 .28 .33 .43 .35 .30 .38 .31 .22 .32 .37 .30 .36 .38 .44 .34 .28 .35 .45 .40 .32 .39 .34 .25 .32 .34 .35 .41 .31 .25 .10 .25 .35
S7 .35 .29 .21 .15 .23 .32 .42 .35 .33 .28 .36 .38 .48 .42 .34 .43 .48 .39 .33 .25 .34 .40 .33 .23 .32 .35 .40 .31 .25 .10 .25 .27 .35
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
exists. The Phase 4U limits are `16 m` cadence, maximum absolute grade `.60 m/m`, maximum grade
angle `atan(.60)=30.96375653207352 degrees`, and maximum adjacent segment-grade change `1.20 m/m`.
Exhaustive integer-hundredth enumeration of the eight rows and all 64 eligible profile boundaries
proves both limits exact. Every row contains one deliberate `.15` normalized rise immediately
followed by a `.15` fall, or its valley mirror, so all eight profiles exercise both the `.60` grade
and `1.20` grade-change bounds. No row adds a reversal, zero-grade bridge, or adjacent pair of
cyclic `16 m` reversal intervals.

For reversal authority, form the 32 cyclic segment grades, delete zero grades without joining or
averaging their magnitudes, and count every sign change in the remaining cyclic sequence. The frozen
Phase 4S predecessor counts are `[6,6,8,8,8,6,8,6]`; the Phase 4T and Phase 4U counts are both
exactly `[12,12,16,16,16,12,16,12]`. Thus each successor doubles its own predecessor and each cyclic
mean spacing is exactly halved from `512/6` to `512/12` or from `512/8` to `512/16` meters. A
reversal's strength is the absolute difference between its two adjacent nonzero grades after the
same zero deletion. The corpus arithmetic median is exactly `.20` in frozen Phase 4S and `.60` in
both Phase 4T and Phase 4U. Phase 4U deliberately extends the strength maximum to `1.20`; strengths
span `.32` through `1.20`. Reversal spacings remain the same irregular multiples of 16 from `16`
through `64 m`, and no profile has two cyclically adjacent `16 m` reversal intervals. This rules out
per-sample sawtooth chatter while preserving intentionally angular peaks, valleys, and intermediate
levels.

Variety is structural rather than a screenshot impression. For each seed in
`[11,39,41,STATIC_WORLD_SEED]`, independently reconstruct 4,096 consecutive canonical vertices
centered on zero and assert: all eight profiles occur; no two adjacent superblocks select the same
profile; no exact vertex period from 1 through 64 exists; each 512 m profile's internal 128 m cuts
differ from `.35`; and both `.10` and `.60` occur in the complete corpus. Record normalized Pearson
autocorrelation at vertex lags `[1,8,16,24,32,40,48,56,64]`. Lag 1 may be high because adjacent
facets are navigable, but the maximum absolute value at every integer lag 16 through 64 is
respectively `.07646434391654663`, `.07656837612996495`, `.07236269854708481`, and
`.0685849311950715`, occurring at respective lags `[32,26,40,60]`; require it below `.09` for every
seed. Mutating the epoch rotation, shuffle index, first/last separation, any profile sample, or
boundary rule must fail this independent reconstruction. These are numeric world properties, not
authored-prose or golden-image assertions.

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
recovery, geometry fixtures, and independent tests consume that function or byte-equivalent
canonical vertices. A retained range is the sorted strict-X union of its clipped 16 m lattice, range
endpoints, and exact structure/column X insertions evaluated from the same interpolation. Eviction
and regeneration in either signed direction reproduce canonical bytes without mutable terrain
history.

### 5.3 Terrain-independent sites and exact local decks

For every signed site index `i`, first derive the route-only nominal center `M_i=36+192*i`. The six
route-only candidate offsets are exactly `[0,8,16,24,32,40] m`. Before any terrain-height read for
any site, compute `q=sampleUnit(seed,17,0)` once for the run and choose the complete candidate-index
order `[0,1,2,3,4,5]` when `q<.5`, otherwise `[0,5,4,3,2,1]`. At exact `.5` the latter order wins.
The order depends only on the normalized seed and is identical for all signed site indexes; it does
not read terrain, a profile ID, a prior rejection, flight evidence, retained state, or play
direction. Candidate ordinal `j` fixes `C_i=M_i+offset[order[j]]` and its complete footprint before
its first terrain read. The nominal candidate is therefore always first and selected without regard
to terrain. Review seeds `[11,39,41,STATIC_WORLD_SEED]` yield stream-17 samples
`[.829028723295778,.2906965466681868,.7745874191168696,.03303934237919748]` and therefore candidate
orders `[1,0,1,0]`; permanent tests recompute rather than copy those decisions.

For a fixed candidate, set `platformLeft=C_i-4.8`, `platformRight=C_i+4.8`,
`buildingLeft=platformRight+2`, and `buildingRight=C_i+13.8`. Then, and only then, derive:

```text
closedFootprint = [C_i-4.8,C_i+13.8]
localNativeMaximum = max(terrainHeightAt(seed,x), x in closedFootprint)
platformTop = localNativeMaximum + 2.5
platformBottom = platformTop - .35
normalizedDeck = (platformTop + 9.2) / 64
```

Because the native surface is piecewise linear, the maximum is evaluated exactly at the two closed
endpoints and every canonical 16 m vertex inside the footprint; no numerical scan or sample spacing
is authoritative. Do not round the deck, consult the next site, flatten native terrain, or share a
global datum. If `normalizedDeck>.5`, reject only this fixed candidate and evaluate the next fixed
candidate. If `normalizedDeck<=.5`, accept it, including exact equality. Candidate exhaustion is a
generation invariant failure, never permission to clamp a deck, lower or resample terrain, skip a
profile, change the seed/order, relax collision, or widen the order. Exhaustive closure proves every
assignment accepts by ordinal 5. Accepted deck tops span `[6.996,22.604] m`, normalized
`[.2530625,.4969375]`, and adjacent signed deltas `[-15.584,14.184] m`. Terrain still reaches
normalized `.10/.60` away from accepted footprints and remains byte-identical after either an
acceptance or rejection; no shelf, cap, blend, corridor relief, foundation, or site-conditioned
terrain branch exists.

The nominal `192 m` cadence retains eight phases modulo 512. Because a candidate may advance by 40
m, four phases remain inside one superblock and four cross one boundary. Enumerate both candidate
orders over all legal profiles: same-superblock phases contribute eight assignments and crossing
phases contribute all 56 ordered distinct adjacent-profile pairs, for `2*(4*8+4*56)=512`
assignments. Exact integer-millimeter distance/deck grouping yields 250 geometry classes over
distances `[152,160,168,176,184,192,200,208,216,224,232] m`, with respective assignment/key counts
`18/12,9/3,2/2,8/8,2/2,359/134,10/9,22/10,15/14,14/14,53/42`. Equality means exact distance and
deck/structure constraints, not similar height or rounded delta. They remain a geometry census, not
a route lookup or exhaustive flight obligation.

Accepted centers remain strictly increasing: adjacent spacing is inclusively `152..232 m`, so the
minimum clear X gap from one complete structure right edge to the next platform left edge is
`152-18.6=133.4 m`. For every seed, endpoint offsets independently bound the complete positive-run
mean in `[192-40/4095,192+40/4095]`, or `[191.99023199023198,192.00976800976802] m`. Signed
regeneration computes each index independently and byte-identically. Across all 8,191 indexes for
seeds `[11,39,41,STATIC_WORLD_SEED]`, every site terminates by ordinal 5 and both spacing bounds
occur. In all four complete positive review runs, sites 0 and 4095 accept nominal centers `36` and
`786276 m`, so each observed mean is exactly `192 m`. Site `4095` leaves exactly `142.2 m` from
nominal `buildingRight=786289.8` to the right rail; even its largest legal fallback leaves
`102.2 m`. Site `-4095` leaves at least `223.2 m` from its platform left edge to the left rail.
Existing final-site and physical-terminus semantics therefore require no change.

The exact `4094 -> 4095` review labels for seeds `[11,39,41,STATIC_WORLD_SEED]` are respectively
`r:192000:16500:9460`, `r:192000:18340:14644`, `r:192000:11228:19860`, and `r:192000:19796:11252`.
Each belongs to the geometry census; final progression directly computes section 10.1's allowance
before the terminal service transaction described in section 7.2.

`STATIC_WORLD_SEED` selects candidate order 0 and site-0 profile S0. Its first candidate is
eligible: center `36`, platform top `9.428`, normalized deck `.2910625`. Static `lander-game.html`,
no-JS recovery, START regeneration, derived opening S0/0, support feet, and collision descriptors
must be byte-equivalent projections of this one authority; markup cannot retain Phase 4S's former
static deck or terrain row.

Each support rail starts at `platformBottom` and ends independently at
`terrainHeightAt(seed,railX)`. Bay count remains the exact existing `.8 m` subdivision and has no
maximum or terrain-band-specific cap: supports extend however long the local deck-to-native-terrain
distance requires. Retention bounds sites and DOM nodes, not the number of members in an individual
required support. Finite closure proves candidate exhaustion cannot occur; production has no
player-visible generation-error branch. A mutation never causes a lower terrain profile, a new
seed/order, a seventh candidate, a relaxed collider, or fallback route. The finite closure observes
column span `[2.15,11.99] m`, at most 15 bays and 33 members in one column, and at most 77 exposed
truss-plus-column members at one accepted site. These are fixture/performance witnesses, not
rendering caps; the exact uncapped formula remains authority.

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
apart, so section 5.2 gives `abs(footA-footD)<=.60*1=.60 m`. The terrain wedge therefore fits inside
`1 by .60 m`, with exact diagonal `sqrt(1+.60^2)=sqrt(1.36)=1.16619037896906 m`. Added rails, ties,
and braces can only subdivide these bounds, and member stroke can only shrink them. The conservative
truss and column colliders are therefore honest: no complete `3.2 m` hull can enter an opening they
reject while the narrower lattice remains visibly open.

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

Set `WORLD_MIN_X=-786432 m`, `WORLD_MAX_X=786432 m`, `MIN_SITE_INDEX=-4095`, `MAX_SITE_INDEX=4095`,
and `TERMINUS_WIDTH=.2 m`. Both world bounds are exact multiples of the 16 m terrain cadence and 192
m nominal route spacing. Canonical terrain contains every lattice vertex and linear segment in the
closed interval `[WORLD_MIN_X,WORLD_MAX_X]` and none beyond it. Both bounds equal `4096*192 m` and
are exact multiples of the 16 m terrain cadence. The pure site helper accepts the complete signed
index interval so signed reconstruction remains meaningful; ordinary mission progression begins at
site `0` and advances through site `4095`. The final site's rightmost structure point is
`786289.8 m`, leaving `142.2 m` of visible native terrain before the right terminus. No profile,
site, deck, route, seed, or retained state chooses either bound.

The finite terrain lattice is exact: vertex indexes run inclusively from `WORLD_MIN_X/16=-49152`
through `WORLD_MAX_X/16=49152`, for 98,305 vertices. Its 98,304 closed segments use their left
vertex index and therefore run from `-49152` through `49151`. Equivalently, segment superblocks run
from `-1536` through `1535`; the vertex at index `49152` is the shared right boundary and not the
start of an out-of-domain segment. These are derived domain facts, not a second terrain authority or
fields duplicated into a fixture.

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

This domain is also the numeric-totality authority. At most 4,096 successful services can occur. For
each one-indexed service, evaluate `ratio=1+0.5**(n-1)` as JavaScript Number: `ratio(53)` is exactly
`1.0000000000000002`, and round-to-nearest-even makes `ratio(54)` and every later value exactly `1`.
The exact dyadic sum of those 4,096 individually projected values is `4098-2^-52`; this is not a
claim about a separately rounded Number accumulator. Section 10.1's maximum direct allowance is
exactly `26.75`. The executable monotone no-burn witness starts `worstFuel=15` and, for `n=1..4096`
in ascending order, performs `worstFuel += 26.75*(1+0.5**(n-1))`; it ends at exactly
`109636.5 < 109637`. Use the conservative integral bound `F=109637`. Existing thrust and fuel
arithmetic then gives total translational acceleration impulse at most
`(90/7)*F=1409618.5714285716 m/s`, total angular impulse at most `80F=8770960 degrees/s`, and
one-step unwrapped angular travel at most `8770960/120=73091.33333333333 degrees`. A deliberately
loose positive-Y bound is `.5*((90/7)*F)^2=993512258458.1635 m < 2^40 m`; terrain contact plus the
corresponding ballistic fall bound keeps the first contact-crossing endpoint inside the same
magnitude. The terminus inner faces and one-step velocity bound keep every collision endpoint within
`786432+11746.828095238097=798178.8280952381 m < 799000 m` absolute X. These are derived
reachable-state proofs, not new clamps, crashes, integration branches, or player-facing limits. They
make every terrain index, rotation-knot count, rational polynomial coefficient, and dyadic isolating
interval in section 9 finite and safely representable. A mutation that treats a non-finite value,
coordinate guard, work budget, or violated derived bound as contact is forbidden; such a state
indicates an implementation defect and fails initialization transactionally before play, never as a
model state or flight transition.

## 6. Projection, camera, and bounded retention

The SVG uses `viewBox="0 0 1000 640"`, `preserveAspectRatio="xMidYMid meet"`, and no intrinsic
minimum width. `#lander-scene-stage` is `position:relative`, has `aspect-ratio:25/16`, and owns that
SVG plus every overlay. `#lander-scene-shell` is a normal-flow column, `width:min(100%,60rem)`, and
contains the stage followed by the controls rail. It has no aspect ratio of its own. This keeps the
scene's exact projection while making the rail a real terrain-separated band rather than an overlay.
Neither box can cause page overflow at 320 CSS pixels or 400 percent zoom. Horizontal scale is
`10 scene units/m`; vertical projection is always `sceneY=548-worldY*10`.

Phase 4U retains the fixed-height invariant. The stage remains a `25/16` box for the entire run; the
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
widen the range to reach an offscreen site. The result stays below the independently generated v10
render-vertex ceiling and reconstructs identical native values from seed and signed global vertex
indexes after eviction, reversal, and return anywhere inside the physical rails. Collision
separately queries global procedural segments across the complete in-domain sweep as section 9
requires. When a terrain or sky key changes, reconcile the fixed nodes once; ordinary frames update
transforms and attributes only. The run retains no discarded terrain or site history beyond
`completedSites` and the latest checkpoint snapshot.

Hard runtime ceilings are two terrain paths, one permanent terminus path, three site groups, eight
debris fragments, 80 descendants under `#lander-world`, one sky group with exactly two path
descendants, the reviewed v10 terrain-vertex ceiling, 64 queued input records, one pointer, one
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
on the pre-merge Chromium machine. Candidate selection, local terrain construction, and direct
allowance arithmetic together must finish below 25 ms at the 95th percentile and 50 ms maximum over
the same witness; record actual results rather than weakening the ceiling.

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
activeSiteId, targetSiteId
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
thrust, allowance, award, collision, or fuel retention. The name must replace `legDepartureFuel`
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
no fuel, allowance, award, checkpoint, collision, or world value.

`createPreflightModel()` and `START` set `refuel=null`. The only non-null interval is the
normal-motion `landed` presentation. Entering `deploying` or `failed`, `RESTART`, Exit/destroy, and
every new mission clear it.

Preflight uses the checked-in site 0 and initial pose but no active run seed or visible fuel. START
creates site 0 as target, `fuel=15`, `fuelGaugeReference=30`, `completedSites=0`,
`refuelRatio=ratio(1)=2`, and the initial approach:
`(x,y,vx,vy,angle,angularVelocity)=(30,32,0.8,-0.4,0,0)`.

The opening reserve has the 16 independent profile/candidate-order fixed-step witnesses in section
10.2. Each starts from this exact pose and fuel `15` and uses only the unchanged command vocabulary.
The test-owned command sequences are offline witnesses, not a runtime assist, planner, award input,
or claim of minimum fuel. Together they cover every eligible first-site outcome under real collision
and landing authority; no retired integer deck tier remains an opening authority.

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
| Any active               | `EXIT`                                  | `preflight`              |
| `flying`                 | unsafe contact with reduced motion      | `failed`                 |
| `flying`                 | safe target contact with reduced motion | `launching`              |

The reduced-motion safe-contact transition atomically applies the complete `landed`, `deploying`,
and `powering` state result and stops launch-ready on the powered pad. There is no `succeeded`,
automatic launch, or terminal deployment state.

Safe contact performs one indivisible service preparation before `landed` renders: for sites
`0..4094`, generate the next site and compute its direct allowance; mark the contacted can
collected; add its award; increment `completedSites`; derive and store the next `refuelRatio`
exactly once; and freeze upright at the deck. Section 5.3's finite closure makes candidate
exhaustion impossible; there is no route-key, proof, or generation-error transaction.

Site `4095` is the sole terminal transaction. It does not call `createSiteForIndex(4096)` or create
a descriptor beyond the physical rail. It otherwise preserves the ordinary can, 300 ms refuel, 900
ms deployment, 1,400 ms power, installed agent, status, and launch-ready transitions. Its sufficient
terminal allowance is exactly section 10.3's direct `22`, using the formula with an absent next leg
represented only here as `deckDelta=0`; the committed award is `22*currentRefuelRatio`, added to
carryover without rounding or clamp. The same transaction sets `activeSiteId=4095`,
`targetSiteId=null`, leaves `generatorCursor=4096`, and increments `completedSites` to `4096`. The
target cue is hidden. After deployment the player may launch, explore the remaining `142.2 m`,
reverse, or contact the visible right terminus. No new outcome state, copy, control, route, or
automatic Exit is introduced. Normal timing for both ordinary and final services is:

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
7. The active platform top is ignored only while `launchCleared=false`. Once both transformed feet
   are strictly above `platformTop+0.05 m`, set `launchCleared=true` and return `flying` in that
   same step without resetting pose, fuel, command, clock, or input. Platform sides, underside, the
   unified truss, NOC, mast, and terrain are never ignored.

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
`refuelRatio=2`, `generatorCursor=1`, `activeSiteId=null`, `targetSiteId=0`, and site 0's exact
uncollected, power-off retained descriptor. Its pose is exactly `(30,32,0.8,-0.4,0,0)`, with no
service, checkpoint, status, or launch flags. The retained window is reconstructed purely from that
same seed and cursor; only `crashOrdinal` remains as run-lifetime presentation bookkeeping. Exit or
ordinary reload discards the checkpoint and gets a fresh seed and zero crash ordinal.

The restore proof takes a canonical projection of the immutable checkpoint immediately after its
first creation and compares it with the post-Retry model after removing only the documented
launch-ready presentation fields. Equality is deep and exact, not approximate. It covers the same
active site and platform ID, centered upright six-component pose, carried post-award `fuel`, equal
`fuelGaugeReference`, target ID, `completedSites`, `refuelRatio`, `generatorCursor`, ordered
retained chunks, and every retained site's can, power, NOC stage, and installed-agent-derived state.
A second Retry after another crash must equal the same projection. Separate counters around the
transition prove no site is generated, can is collected, award is added, base progress advances, or
NOC is powered during either restore.

## 8. Physics, fuel, and fixed-step clock

### 8.1 Constants and integration

Constants are named exports and are not configurable at runtime:

```js
export const TRANSLATIONAL_MASS_NUMERATOR = 7;
export const TRANSLATIONAL_MASS_DENOMINATOR = 10;
export const TRANSLATIONAL_MASS = TRANSLATIONAL_MASS_NUMERATOR / TRANSLATIONAL_MASS_DENOMINATOR;
export const ENGINE_FORCE_COEFFICIENT = 9;
export const GRAVITY_FORCE_COEFFICIENT = 3;
export const STEP_SECONDS = 1 / 120;
export const MAX_FRAME_SECONDS = 0.1;
export const MAX_CATCH_UP_STEPS = 12;
export const GRAVITY = 30 / 7;
export const ENGINE_ACCELERATION = 90 / 7;
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

The rational mass authority is exactly `7/10`; do not recover it from a rounded decimal field or
serialize a second independently editable value. `ENGINE_FORCE_COEFFICIENT` and
`GRAVITY_FORCE_COEFFICIENT` preserve the Phase 4T mathematical force coefficients `9` and `3`. The
acceleration constants are the direct Number expressions `90/7` and `30/7`, and their Number ratio
is exactly `GRAVITY/ENGINE_ACCELERATION===1/3`. In the intended operation order,
`ENGINE_ACCELERATION*7/10===9` and `GRAVITY*7/10===3`; the differently associated raw expression
`.7*(30/7)` evaluates to `2.9999999999999996` and is not an acceptance assertion. Fuel has no mass
effect, torque remains exactly `80`, and no angular-inertia coefficient exists.

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
rotation by shortest-arc subtraction. Player flight, manual departure, and representative offline
flights share `effectiveThrust` and `integrateStep`.

At upright full steering with collective, the exact Number axial acceleration is
`(90/7)*.8*cos(30 degrees)=8.907689867497085`, or `48.112522432468824%` of straight collective's
`(90/7)*1.44=18.514285714285716`; lateral acceleration is `5.142857142857142`. At upright turn-only,
axial acceleration is `(90/7)*.375*cos(30 degrees)=4.175479625389258`, less than gravity `30/7`,
while lateral acceleration is `2.410714285714285`. These independently computed values are
acceptance bounds, not merely desired feel.

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
unwrapped direction. Section 5.4 bounds the sequence at 73,094 entries: an interval of length
`73091.33333333333` degrees crosses at most 73,092 integer degrees, plus its two endpoints.
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
`(seed,signedVertexIndex)`. For closed enclosure X bounds `[L,H]`, compute the inclusive left-vertex
index range exactly as

```text
firstSegmentIndex = max(-49152, ceil(L/16)-1)
lastSegmentIndex  = min( 49151, floor(H/16))
```

Enumerate every integer in that range when `firstSegmentIndex<=lastSegmentIndex`; a reversed range
is empty. The `-1` preserves the segment ending at an enclosure whose left edge is exactly a lattice
vertex. Thus a point enclosure at `WORLD_MIN_X` selects only `-49152`, one at `0` selects `-1,0`,
one at `WORLD_MAX_X` selects only `49151`, and the complete world selects all 98,304 segments
`-49152..49151`. Index `49152` is a terminal vertex, never a segment left index. Retained terrain
vertices remain a bounded DOM projection and are never a collider boundary. Traverse candidate
segments in increasing left-index order and retain only the current candidate plus the current
earliest contact; do not allocate the 98,304-segment full-domain range. Add both terminus rays
whenever the enclosure reaches their inner faces. Structures are deliberately different: collision
considers exactly the authoritative maximum-three retained site descriptors—active, target, and
preceding—and never synthesizes future or evicted sites. From each retained descriptor, derive its
platform, continuous truss envelope, all three column envelopes, NOC body, and solid `.5 m` mast
from section 5.3, never from controller DOM coordinates.

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
the same deterministic settled pose before the allowance, award, or checkpoint commits. This makes
both transformed feet remain on the closed deck span and makes the later player-reachable launch
departure use the canonical checkpoint pose. Gas-can art and antenna signal arcs do not collide.
Contact with a consumed/powered site's platform during a later leg is unsafe; only the current
target can complete a leg.

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

## 10. Constant-time allowance and representative flight evidence

### 10.1 Runtime allowance authority

Phase 4U removes route schedules from runtime. After the next site descriptor exists, the model
computes only the ordered deck delta and this expression:

```text
FUEL_QUANTUM = .05
quantumCeil(x) = Math.ceil(x / FUEL_QUANTUM) * FUEL_QUANTUM
deckDelta = target.platformTop - origin.platformTop
allowance = quantumCeil(22 + Math.max(0,deckDelta) / 3)
```

This is O(1) arithmetic over two already-generated descriptors. It does not derive a distance or
pair key, construct a collision envelope, search commands, simulate a flight, replay a schedule,
scan a catalog, or consult retained/rendered terrain. It cannot fail because a route key, schedule,
proof record, or generated module is missing. Candidate exhaustion is independently impossible over
section 5.3's finite geometry authority; production does not project it as a route
`generation-error` state. Invalid non-finite internal data is a programming invariant caught by
tests, not a player-visible mission branch or an invitation to change terrain.

The literal `22` is deliberately conservative. It is the next whole-unit round-up above the largest
controller burn observed in the reviewed Phase 4U reference study, `21.10175000000128`. That
observation is neither a universal route minimum nor proof that every possible control sequence or
generated route consumes at most that value. The runtime formula is a simple fuel prediction policy.
Descent receives no credit. The positive-climb term retains the exact ratio
`GRAVITY/ENGINE_ACCELERATION=(30/7)/(90/7)=1/3`; no floating force-product reconstruction is
authoritative. With section 5.3's exact deck-delta range, the largest arithmetic result is
`quantumCeil(22+14.184000000000003/3)=26.75`.

No `targetRouteProof`, `routePairKey`, proof catalog, schedule digest, generated proof import,
defensive replay, runtime search, or proof-derived failure belongs to the model, checkpoint, site,
controller, static markup, or built JavaScript. Historical Phase 4R-4T route fixtures and tools may
be deleted when they have no remaining current consumer. They are never Phase 4U bootstrap input.

### 10.2 Geometry parity and non-exhaustive flight evidence

Keep only the smallest checked parity seam needed to prevent the independent terrain/site authority
and production from drifting: `website/tests/fixtures/lander-route-geometry-v10.json`. Despite its
historical filename, it contains geometry and physics only—no assignments, pair keys, envelopes,
commands, schedules, openings, terminal transaction, or proof records. Its schema is exactly
`agw-lander-route-geometry/v10`; `deriverVersion` is exactly `agw-lander-geometry-deriver/v1`. Its
top-level fields are exactly
`schema,deriverVersion,terrain,site,structure,world,collision,physics,geometryDigest`.

The payload contains the exact section 5.2-5.4 and 8-9 constants: eight profile rows, selector, 16 m
cadence, `.60/1.20` grade bounds, six offsets and both run-wide candidate orders, `.5` deck cap,
closed footprint, local `+2.5 m` clearance, rational mass and force/acceleration constants, physical
rails, site limits, collision recipe, margin, and angle-knot authority. Canonical JSON recursively
sorts object keys, preserves pinned arrays, uses compact `JSON.stringify`, and hashes UTF-8 bytes
with lowercase SHA-256. `geometryDigest` hashes the complete object with only that field omitted.
The file adds exactly one unhashed trailing LF. Its implementation-generated digest remains pending
atomic generation and independent review; this LLD does not fabricate it.

A focused standard-library Node geometry generator accepts exactly:

```text
node website/tools/derive_lander_geometry.mjs --output PATH [--verify PATH]
```

It writes this one fixture atomically and verifies a checked fixture byte-for-byte. Unknown,
reordered, or missing flags exit 2, generation or verification failure exits 1, and success exits 0.
Generator and test independently reconstruct section 5.3's 512 finite profile/order assignments,
six-candidate termination, distance/deck census, signed sites, four complete positive missions,
final-site clearance, and `98,305/98,304` world vertex/segment counts. Those are geometry checks,
not route records or schedule inputs. No source projection CLI or generated JavaScript module
exists.

Flight evidence is intentionally small, offline, non-exhaustive, and non-optimal. A test-only
reference harness uses the production command vocabulary, exact 120 Hz integrator, exact collision
authority, and unchanged landing envelope. It replays all 16 initial openings from
`(30,32,.8,-.4,0,0)` with fuel 15 and `originSiteId=null`, plus these four deterministically
selected representative geometries:

| Class            | Geometry               | Observed safe contact                                                   |
| ---------------- | ---------------------- | ----------------------------------------------------------------------- |
| Closest spacing  | `r:152000:11924:17412` | step 6747; burn `20.160583333334753`; max hull top `40.116152267342486` |
| Farthest spacing | `r:232000:9460:15412`  | step 6820; burn `20.957750000001486`; max hull top `38.26272108358484`  |
| Maximum rise     | `r:192000:6996:21180`  | step 6780; burn `20.38125000000147`; max hull top `39.558359400885664`  |
| Maximum fall     | `r:192000:22580:6996`  | step 6805; burn `20.35175000000132`; max hull top `50.77215226734249`   |

The labels identify review evidence only; production never constructs or consumes them. The harness
derives each geometry independently, replays an explicit test-owned command sequence, requires
actual safe contact, and proves burn does not exceed that leg's runtime allowance. The 16
human-readable `(center,deck,contactStep,burn,reserve)` opening summaries remain:

```text
S0/0 (36,9.428,2781,8.6325,6.3675)    S0/1 (36,9.428,2781,8.6325,6.3675)
S1/0 (52,22.58,2717,8.3415,6.6585)    S1/1 (76,15.412,2785,8.7255,6.2745)
S2/0 (36,16.276,2775,8.529,6.471)     S2/1 (36,16.276,2775,8.529,6.471)
S3/0 (60,20.436,2719,8.345,6.655)     S3/1 (68,21.252,2711,8.2855,6.7145)
S4/0 (36,20.396,2711,8.3015,6.6985)   S4/1 (36,20.396,2711,8.3015,6.6985)
S5/0 (52,22.58,2717,8.3415,6.6585)    S5/1 (76,21.788,2705,8.405,6.595)
S6/0 (36,20.82,2692,8.309,6.691)      S6/1 (36,20.82,2692,8.309,6.691)
S7/0 (36,6.996,2795,8.5805,6.4195)    S7/1 (36,6.996,2795,8.5805,6.4195)
```

These 20 flights are regression examples, not a generated catalog, exact fuel predictor, exhaustive
reachability proof, global optimum, release gate over all 250 geometry classes, or runtime input.
Release acceptance instead requires deterministic geometry closure, all four 4,096-site missions
without site-generation failure, Retry/checkpoint identity, terminal service/rail contact, and
bounded browser lifecycle work.

### 10.3 Award, ratio, carryover, and terminal service

For one-indexed successfully powered base number `n`, retain exactly `ratio(n)=1+0.5**(n-1)`. On
safe contact, compute the next site's direct allowance from section 10.1, validate the stored ratio
against `refuelRatioForBase(completedSites+1)`, compute `award=allowance*ratio`, and add it to
carried fuel without rounding or a capacity clamp. Progress, ratio advancement, can collection,
checkpoint, gauge reference, and mission transition commit once and atomically. Different carried
reserves do not alter allowance; only the already-established ratio changes the award. The opening
reserve still funds site 0, and site 0's can funds leg 1.

At final site `4095`, section 7.2 supplies `deckDelta=0`, so direct allowance and award are `22`
because the late-run ratio is exactly `1`. Complete service, set `targetSiteId=null`, and hide the
cue without creating site 4096 or looking up/replaying a route. The final award preserves ordinary
carryover and may fund voluntary flight into the visible right terminus.

Retain the independent engine-arithmetic test: a deliberately bounded reserve proves exact
partial-step exhaustion and zero subsequent thrust, while values on both sides of each `.05`
boundary pin upward quantum rounding. It does not assert that one quantum less than this
conservative prediction makes a route impossible. For the finite-world numeric-domain witness,
`ratio(n)` becomes exact Number 1 at `n=54`; an ascending 4,096-award no-burn loop using the largest
`26.75` allowance yields exact Number `109636.5`. Set the conservative finite fuel-domain bound to
`F=109637`; the loop, not an analytic real-number sum, is authority.

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
  `data-banner="none|deployed|crashed"` from the exact projections below;
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
`failed`; and `none` otherwise. Banner selection never branches on repository-authored status
wording. For `deployed` or `crashed`, `#lander-outcome` is positioned exactly with
`position:absolute`, `inset-block-start:clamp(0.5rem,2vw,1rem)`, `inset-inline-start:50%`,
`inset-inline-end:auto`, `transform:translateX(-50%)`, and
`inline-size:min(32rem,calc(100% - 6rem))`. It is a column aligned to center. `#lander-status` is a
centered three-pixel graphite-bordered, sky-backed arcade panel; deployed uses
`box-shadow:inset 0 -3px 0 #2ed49b,3px 3px 0 #292b30`, crashed substitutes `#ff5a36`, and the exact
text independently names the outcome. Retry follows status directly in DOM and visual flow with a
`0.5rem` block-start margin. It is visible only for `crashed`, where it appears beneath `Crashed!`;
deployed banners contain no action. With `data-banner="none"`, status uses the established visually
hidden clipping recipe and Retry stays hidden and disabled. The outcome is pointer-transparent
except that the visible Retry itself restores `pointer-events:auto`; its descendant events still
reach the stage and are rejected by section 11 before any flight-input effect.

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

Numerical physics tests use tolerance `1e-10`; representative offline flight replay uses the pinned
`1e-9` reporting precision from section 10.2. Machine-owned identifiers and enum strings, integers,
states, seed values, DOM order, and serialized world descriptors are exact; section 4's authored
prose is human-reviewed rather than asserted. Every schedule includes an explicit final callback.

| Vector                 | Input                                                                                                                              | Expected result                                                                                                                                                              |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Gravity, 120 steps     | `(10,30,0,0)`, zero angle/engines, fuel 30                                                                                         | `x=10`, `y=27.839285714285715`, `vx=0`, `vy=-4.285714285714286`, fuel `30`                                                                                                   |
| Collective, 120 steps  | Same pose, engines `(0.72,0.72)`                                                                                                   | `y=37.17357142857143`, `vy=14.228571428571431`, angle/x unchanged, fuel `28.56`                                                                                              |
| Turn-only vector       | One step from same pose, raw engines `(0,0.375)`, `s=-1`                                                                           | `ax=-2.410714285714285`, `ay=-0.11023466032502771`, `omega=-0.25`, angle `-0.00208333333333`, fuel `29.996875`                                                               |
| Combined turn vector   | One step from same pose, raw engines `(0.2125,0.5875)`, `s=-1`                                                                     | `ax=-5.142857142857142`, `ay=4.6219755817828`, `omega=-0.25`, angle `-0.00208333333333`, fuel `29.993333333333332`                                                           |
| Angular assist         | One step, angle `0`, omega `15`, raw engines `(0.72,0.72)`                                                                         | engines `(0.66,0.78)`, `s=0`, omega `14.92`, angle `0.124333333333`, fuel `29.988`; total thrust unchanged                                                                   |
| Vacuum coast           | One step, angle `0`, omega `15`, zero engines                                                                                      | omega remains `15`, angle `0.125`, `vy=-0.03571428571428571`; no translational or angular damping                                                                            |
| Exhaustion             | Fuel `0.005`, one step, engines `(1,1)`                                                                                            | Effective engines `(0.3,0.3)`, fuel exactly `0`                                                                                                                              |
| Pointer vectors        | Rightward normalized drag `m=0,0.5,1`                                                                                              | `(.72,.72)`, `(.65375,.46625)`, `(.5875,.2125)`; leftward values mirror exactly                                                                                              |
| Mixed input ceiling    | Keyboard collective plus pointer full right                                                                                        | pointer owns `s=1`; engines `(.5875,.2125)`, total `.8`, never component-combined                                                                                            |
| Keyboard steer owner   | Keyboard left plus pointer full right                                                                                              | keyboard owns `s=-1`; engines `(.2125,.5875)`, total `.8`                                                                                                                    |
| Canceled steer owner   | Both keyboard steers plus pointer half right                                                                                       | keyboard cancels; pointer owns `s=.5`; engines `(.65375,.46625)`, total `1.12`                                                                                               |
| Empty-fuel direction   | Fuel `0`, raw engines `(.5875,.2125)`, retained physics `s=1`                                                                      | effective engines `(0,0)` and stored/rendered `commanded.vectorAngle=0`                                                                                                      |
| Plumes                 | `u=0,0.5,1`                                                                                                                        | scales `0.08,0.54,1`; opacities `0.25,0.625,1`                                                                                                                               |
| First site             | Any normalized seed                                                                                                                | ID `0`; nominal center `36`; bounded order selected before terrain; accepted center/deck satisfy section 5.3 exactly                                                         |
| Site progression       | Signed indexes `-4095..4095`; eight nominal 192/512 m phases                                                                       | `M_i=36+192*i`; accepted spacing `152..232`; direct O(1) allowance; at most six fixed candidates                                                                             |
| Physical termini       | Approach exact X `-786432` and `786432` from inside at low and high altitude                                                       | permanent rails are visible first; first isolated rail contact crashes; retained range edges remain inert                                                                    |
| Procedural index range | Closed X enclosures at `WORLD_MIN_X`, `0`, `WORLD_MAX_X`, and the complete world                                                   | segment left indexes `[-49152]`, `[-1,0]`, `[49151]`, and all 98,304 integers `-49152..49151`; terminal vertices are `-49152..49152`                                         |
| Final service          | Power site `4095` with ratio `1` and arbitrary carryover                                                                           | award `22`; completed/cursor `4096`; active `4095`; target/cue null; ordinary deploy/checkpoint/launch                                                                       |
| Terrain continuity     | Retained range crossing chunks, both sites, and all twelve column-rail feet                                                        | one strict-X chain; boundary Ys equal; two render paths; open stroke has no floor/vertical/closing segment                                                                   |
| Structure parity       | Static and dynamic site with platform top `p`                                                                                      | one 18.6 m path; 14 fixed truss members plus exactly three bounded variable lattice columns                                                                                  |
| Truss envelope         | Relative chords `[-4.8,13.8] x [-1.1,-.35]`, member width `.2 m`                                                                   | collider `[-4.9,13.9] x [-1.2,-.25]`; deck/NOC overlap; top remains `.25 m` below landing face                                                                               |
| Column envelopes       | rail pairs `0/1,8.8/9.8,17.6/18.6`; six native feet; top `p-.35`                                                                   | each collider is the exact stroked axis-aligned box through its lower foot; all members contained and joined                                                                 |
| Aperture bounds        | Raw truss, `.8 m` lattice bays, and the native-slope wedge                                                                         | diameters `3.1894356867634124`, `1.2806248474865698`, and at most `1.16619037896906`, all below hull `3.2`                                                                   |
| Connected clear face   | Every independently split face for each pinned site descriptor                                                                     | actual maximum axis-aligned envelope equals fixture fields; `hypot(width,height)<=3.1894356867634124`                                                                        |
| Opening gauge          | Fresh run `fuel=15`, `fuelGaugeReference=30`                                                                                       | exact level `.5`, exact accessible reserve `15.0`; no cap or hidden extra fuel                                                                                               |
| Later gauge            | `fuel=37.5`, `fuelGaugeReference=50`, then checkpoint restore                                                                      | level `.75`, level `ready`; restore reproduces both values and never caps fuel                                                                                               |
| Gauge contrast         | danger/caution/ready against `#20232a`; gauge level zero                                                                           | ratios `5.068/8.584/8.243`; graphite boundary plus colored inset remain visible with zero-height fill                                                                        |
| Refuel projection      | pre-award level `.25`; normal landed time `0,.15,.299,.3 s`                                                                        | levels `.25,.625,.9975,1`; one can follows the same linear progress and is absent after `.3`                                                                                 |
| Refuel CSS frame       | stage rect `(100,50,1000,640)`, can scene `(130,433)`, gauge rect `(120,70,16,112)`, `p=.25`                                       | viewport can `(230,483)`, local endpoints `(130,433)` to `(28,76)`, transfer center `(104.5,343.75)`                                                                         |
| Transfer silhouette    | DPR 1, integer CSS-pixel center; computed `::after` plus paired on/off `20 by 22` crops                                            | six pinned layers/sizes/positions/colors; probes hit every outer/inner part and `(0,0)`/`(19,21)` match baseline                                                             |
| Reduced refuel         | Same contact with reduced motion                                                                                                   | full model/fuel text/gauge/checkpoint atomically; `refuel=null`, no transfer pseudo-element                                                                                  |
| Launch-ready hold      | 10 seconds zero or steer-only input after power                                                                                    | centered pose, fuel, mission time, zero command, and status remain unchanged                                                                                                 |
| Manual departure       | Launch-ready plus Space/Up, either plus vi/arrow steer, pointer/touch hold, or eligible tap                                        | every qualifying path uses the ordinary mixer; first step burns/integrates; `flying` starts only after `.05 m`                                                               |
| NOC stages             | Power sequence at `0,.2,.4,.6,.8,1,1.2,1.4 s`                                                                                      | stages `0..7`: installed agent at stage 1, four bars, then three arches; banner only at final stage                                                                          |
| Agent travel           | Deploying time `0,.225,.45,.675,.899,.9 s`; then hide document for `.3 s` at `.45`                                                 | progress `0,.25,.5,.75,.998888...,null`; hidden interval freezes `.5`; power still begins exactly at `.9`                                                                    |
| Installed retention    | Powered sites retained through next leg, crash, and two checkpoint restores                                                        | each existing NOC-entry path stays installed; exact world count remains 76 and no can/power state duplicates                                                                 |
| Outcome/action rail    | Launch-ready, then failed                                                                                                          | banner-only deployed state; crashed status plus Retry; Exit stays bottom-right in the active rail                                                                            |
| Interactive pointer    | `pointerdown` targets Retry descendant and Exit descendant, then native click                                                      | Retry guard has no stage flight effect; Exit cannot reach stage; each native click runs exactly once                                                                         |
| Interactive keyboard   | Focus Exit/Retry; target each button or nested span with Space, Enter, arrows, `h`, and `l`                                        | no flight prevention/held edge/queue/thrust; Space/Enter run one native action; arrows/`h`/`l` run no action                                                                 |
| Outside-shell keys     | Active mission; target header, breadcrumb, and descendants with Escape, `r`, and flight keys                                       | no prevention, focus/state/action/input/model change; outside keyup is also inert after focusout clears input                                                                |
| Controls lines         | 320 px and 400%-equivalent layouts; keyboard child then touch child                                                                | one client rect per line; every relevant `scrollWidth<=clientWidth`; Exit in row 2; no authored-copy assertion                                                               |
| Refuel ratio           | Base `n=1,2,3,4`; test minimum `8`; carried fuel `7,20,5,4`                                                                        | ratios `2,1.5,1.25,1.125`; awards `16,12,10,9`; reserves `23,32,15,13`; next ratios direct from `n+1`                                                                        |
| Ratio precision        | Direct Number formula at bases `52,53,54,100`                                                                                      | `1.0000000000000004`, `1.0000000000000002`, `1`, `1`; never below `1`, no bound or arbitrary precision                                                                       |
| Safe inclusive edge    | Target top; `vx=2.2,vy=-3.6,angle=-18,omega=26`                                                                                    | safe contact                                                                                                                                                                 |
| Unsafe epsilon         | Four contacts, each increasing exactly one boundary magnitude by `1e-9`                                                            | each is unsafe; mirrored absolute-value signs and positive-`vy` rejection are independently covered                                                                          |
| Swept unsafe equality  | Hull only grazes terrain/truss/column/NOC/mast between step endpoints                                                              | repeated polynomial root reconstructs actual closed unexpanded contact; adjacent `.02 m` candidate samples remain clear                                                      |
| Target-top separation  | Safe descent over deck center; exact tangent; near miss; former depth/time cutoff vector                                           | descent/tangent use isolated actual roots; near miss/cutoff enclosure is clear, never a synthetic crash                                                                      |
| Large procedural sweep | Finite low sweeps centered at `x=-600000` and `x=600000`, each across more than 64 segments and beyond both retained terrain edges | earliest global terrain contact in both expanded-world halves; no retained-edge fall-through and no `overspeed` result                                                       |
| Large empty sweep      | Same X/angular travel wholly inside the rails with hull above ordinary candidate maxima                                            | O(1) broad-phase clear; no vertex/knot allocation, work cap, or synthetic result                                                                                             |
| Unwrapped rotation     | Sweeps of `181`, `360`, and `721` degrees with a thin contact only during the positive unwrapped affine-knot sequence              | exact unwrapped angular travel finds contact; shortest-arc mutation misses and fails                                                                                         |
| Maximum knot stream    | Exact maximum-reachable `73091.33333333333`-degree travel with a thin contact confined to the final affine slab                    | streams exactly 73,094 knots in each direction, returns the same actual contact, retains no knot-count-sized array, and leaves DOM/listener/model lifecycle counts unchanged |
| Empty-fuel ballistic   | Exhaust fuel above the former `y=56` ceiling, coast, then return under gravity                                                     | remains flying while clear, then crashes only at the first concrete swept contact                                                                                            |
| Frame equivalence      | Initial approach, no input, callbacks to 1,000 ms at 30, 60, and 120Hz                                                             | 120 steps; `x=30.8`, `y=29.43928571428572`, `vx=0.8`, `vy=-4.685714285714279`, fuel `15`                                                                                     |
| First landing          | All 16 profile/candidate-order openings; opening fuel `15`                                                                         | safe contacts at steps `2692..2795`; at least `6.2744999999995095` fuel remains                                                                                              |
| Free exploration       | Cross `x=-5`, `101`, target right edge, former ceiling, then reverse inside the rails                                              | stays flying absent real contact; camera continuous; cue right/left/right; progress unchanged                                                                                |
| Sky parallax           | Same seed, camera left `0,50,-50`; five derived sky chunks                                                                         | transform `0,-120,120 px`; 20 stars and 1-2 landmarks; two path nodes and exact regeneration on return                                                                       |
| Checkpoint replay      | Award, manual launch, crash, Retry twice                                                                                           | exact deep checkpoint projection both times; no can, award, ratio, route, power, or progress duplication                                                                     |
| Initial Retry          | Crash before first powered base, then click Retry and later use `r`                                                                | exact same-seed initial pose/site/window/fuel/progress/ratio; shell focus; no synthesized input                                                                              |
| Allowance arithmetic   | Deck deltas below zero, zero, and maximum rise plus independent engine exhaustion vectors                                          | direct base `22`; exact positive climb surcharge/quantum ceiling; O(1), carryover preserved, partial final burn then zero thrust                                             |
| Short-tap capture      | Down at `0`, eligible up at `20`; release synchronously emits lost capture                                                         | token/deadline exist before release; pulse remains through `139.999`, ends once at `140`; later loss is no-op                                                                |
| Input overflow         | 65 alternating edges before one step at 30, 60, and 120 Hz                                                                         | queue becomes one next-step physical-state snapshot; all frame schedules produce the same result                                                                             |
| Long run               | 100 successful deterministic sites                                                                                                 | ratios are non-increasing and `>=1`; O(1) direct formula; bounded nodes/edges; exact reserve accounting                                                                      |

The 16 first-landing test vectors pin their last-clear and contact poses in test-owned data rather
than a runtime fixture. Tests compare contact kind, step, pre-award reserve, and pose with `1e-10`
tolerance; none calls a production helper to build its expected schedule.

### 14.1 Phase 4U feasibility and pinned terrain vectors

Before this amendment, disposable independent tooling froze section 5.2's successor rows and
reconstructed every Phase 4T/4U grade exactly in integer hundredths. Both generations' counts are
`12/12/16/16/16/12/16/12`; cyclic means and reversal placements are unchanged. Reversal-strength
median remains `.60`, maximum grade/change intentionally rise to exact `.60/1.20`, and the corpus
reaches normalized `.10/.60`. Four independent centered 4,096-vertex reconstructions contain all
profiles, no adjacent repeats, no exact period through 64 vertices, and the section 5.2
autocorrelation maxima below `.09`. Tests recompute numeric authority rather than authored prose or
screenshots.

Disposable Chromium rendered seed 11 at `1000 by 780`, seed 41 at `320 by 780`, static at the real
`320 by 240` short-height viewport, and seed 39 at touch-landscape `667 by 320`. The disposable page
declares `width=device-width,initial-scale=1`; the refreshed seed-39 capture used CDP mobile
emulation with DPR 1 and five touch points. Window inner size, visual viewport, root and body
client/scroll metrics, CDP layout viewport, and CDP content size were all exactly `667 by 320`, with
visual scale `1`, scroll `(0,0)`, and footer bottom `320`. Direct inspection found strict
straight/miter facets, intentionally sharper irregular peaks and valleys without per-sample chatter,
exact capped local decks, and supports ending at untouched native feet. The other three disposable
viewports likewise reported exact client/scroll equality and zero scroll. These screenshots are
qualitative review artifacts only, not shipped golden images or prose-policing tests. Implementation
must repeat the witness against the actual shared Lander/404 artifact.

Independent candidate closure covers both seeded orders, eight nominal phases, 512 assignments, 250
distance/deck geometry classes, and all ordinals through the terminating sixth candidate. Accepted
normalized decks span `.2530625..4969375`; native terrain still reaches `.6`. Distances span
`152..232 m` and deltas `-15.584..14.184 m`. The earlier reference study observed maximum controller
burn `21.10175000000128` at `r:232000:17412:13684`; section 10 deliberately rounds its conservative
runtime base to `22` without claiming exhaustive prediction or optimality. All 16 openings pass from
the unchanged pose and fuel with reserve at least `6.2744999999995095`. All signed sites for the
four review seeds terminate through index `+/-4095`, preserve `152..232 m` spacing, and leave site
4095's `142.2 m` nominal final-rail gap, or at least `102.2 m` after the largest permitted fallback.
No control, angular acceleration, landing, fuel-consumption, ratio, collision, finite-world, copy,
or scrolling change is required; only R31's explicit terrain, pacing, translational mass, and direct
allowance authority change.

Phase 4U's maximum direct allowance updates section 5.4's bound: the exact ascending Number witness
with no intervening burn ends at `109636.5`, so `F=109637`; maximum one-step translation is
`((90/7)*F+.8)/120=11746.828095238097 m`, maximum unwrapped travel is `73091.33333333333` degrees,
the stream has at most 73,094 knots, and `.5*((90/7)*F)**2=993512258458.1635<2^40`. These are
proof/performance witnesses, not gameplay limits. Permanent geometry generation and production
parity remain final authority before the fixture is accepted.

## 15. Verification matrix

The exact local demo remains
`python3 website/build.py --repo-root . --output /tmp/agentworks-site-demo --site-base /`, followed
by `python3 -m http.server --directory /tmp/agentworks-site-demo 8000`. Game work opens `/lander/`;
fallback acceptance opens `/404.html`.

The complete artifact contains exactly these 14 files:

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
static/lander-collision.js
static/lander-world.js
static/lander-model.js
static/lander-game.js
static/onboarding-copy.js
```

Final ordinary generation and independent verification pin only newly generated v10 geometry bytes
and `geometryDigest` atomically. No derived schedule fixture, route record, generated proof source,
proof/world/output digest, or terminal record is present.

| Layer                                                                   | Required coverage                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ----------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `node --test website/tests/lander-world.test.mjs`                       | Independently reconstruct the eight global profiles, strict 16 m X cadence, exact normalized mapping/band, linear interpolation, miter presentation, exact `.60/1.20` bounds, both candidate orders, all 512 assignments and 250 geometry classes, closed-footprint maximum, `+2.5 m` deck equality, `.5` cap, rejection/termination, and signed regeneration. Pin exact world/site bounds, rails, final-site clearance, 98,305 terrain vertices, 98,304 segments, and native-foot lattice. Mutation-kill terrain-conditioned proposal/order, a seventh candidate, wrong `>.5` tie, clamp/lowering, curves, sawtooth, short period, rounding, a global datum, retained-edge collider, lattice-bound drift, missing terminus, vertical terrain edge, or foot drift.                                                                                                                                                          |
| `node --test website/tests/lander-phase4t.test.mjs`                     | Independently pair every frozen/new profile and prove doubled sign reversals, halved cyclic means, `.20 -> .60` median strength, exact `.40/.80`, no adjacent 16 m reversal intervals, selector variety, candidate-order independence, six-candidate termination, exact cap/max/feet, spacing classes, Retry parity, and final-rail clearance. Kill a profile/candidate/order/threshold/normalization mutation without asserting authored prose.                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `node --test website/tests/lander-phase4u.test.mjs`                     | Independently compare profile rows; prove retained reversals, exact `.60/1.20`, selector/band identity, eight phases, both orders, all 512 assignments/250 geometry classes, six-candidate termination, 192-centered complete runs, exact deck cap/max/feet, rational mass/force vectors, four final-site labels, expanded rails, direct `quantumCeil(22+max(0,delta)/3)`, unchanged ratio/carryover, and absence of runtime search/replay/catalog/pair-key failure. Kill each authority structurally or numerically without asserting authored prose.                                                                                                                                                                                                                                                                                                                                                                      |
| `node --test website/tests/lander-model.test.mjs`                       | Retain unchanged inclusive `2.2/3.6/18/26` landing limits, engine exhaustion arithmetic, fuel, ratio, checkpoint, Retry, zero-fuel, deployment, reduced-motion, hidden-time, collision integration, input, and scheduler vectors without growing this near-ceiling module. No assertion encodes authored prose.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `node --test website/tests/lander-phase4s.test.mjs`                     | Retain exact collision/terminus authority while pinning ratio Numbers at `n=53/54`, maximum allowance `26.75`, ascending 4,096-award no-burn accumulation `109636.5`, and `F=109637`; kill expansion-as-impact, endpoint-only contact, root/tangency/overlap loss, stalled midpoint, shortest-arc knots, retained arrays, invisible rails, terminal award/cursor/null-target drift, collision/world concatenation, analytic sum substitution, or a 15th artifact. Exercise both signed maximum-reachable streams at exactly 73,094 knots, final-slab contact, full-domain streamed terrain, and earliest-contact parity.                                                                                                                                                                                                                                                                                                    |
| `node --test website/tests/lander-phase4l.test.mjs`                     | Mutation-sensitive controller/DOM tests pin exactly two controls-line children in keyboard/touch order, Retry label-source and hint structure without asserting text, internal `RESTART` dispatch, crash focus stability, click/`r` teardown-render-focus order, shell focus with `preventScroll`, and no synthesized input. Existing outside-shell and native-button rejection coverage remains exact.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `node --test website/tests/lander-phase4m.test.mjs`                     | Mutation-sensitive controller/DOM tests pin five sky chunks, 20 stars, one or two deterministic landmarks in exactly two paths, the complete two-arc crescent, all three one/two-ring profiles, exact circle/ellipse intersections, omitted rear-center arcs, and complete foreground arcs. They retain static/dynamic descriptor equality, bounded reconciliation, `.24` parallax transforms, negative/positive camera following, bidirectional cue changes, pass/reverse/return, and the historical rejection of a generic horizontal-bound failure; Phase 4S's focused test owns the exact two-rail contact and camera-clamp exception. They also pin the exact opening half-gauge, post-award full reference without cap, `.9 s` deploy travel, unchanged refuel/power timing, hidden-time freeze, reduced-motion atomic projection, and structural copy/link/accessibility sources without embedding authored wording. |
| Geometry CLI fixture verification                                       | Generate and verify only schema `agw-lander-route-geometry/v10`; derive exact vertex/segment bounds and `98305/98304` counts, enumerate 512 assignments and 250 geometry classes, both candidate orders, six-candidate termination, complete signed/final sequences, and one implementation-generated geometry digest. Reject schedule fields, route records, proof/output digests, source projection, catalog, bootstrap, or undeclared input.                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `python -m unittest discover -s website/tests -p 'test_*.py'`           | Validator pins one shared fragment, structure, accessible-name sources, geometry-v10 schema, exact `+/-786432` world, derived `-49152..49152` vertex authority, direct model allowance, no generated proof import/module or build composition, and exact 14-file artifact/module DAG. It rejects Phase 4P curves/global datum/site-conditioned terrain/vertical camera/ceiling/overspeed and does not assert authored prose.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Automated Chromium projection witness                                   | At exact `1000 by 780`, `320 by 780`, real `320 by 240`, and touch `667 by 320`, assert requested inner dimensions and width/height client/scroll equality before and after lifecycle with zero scroll. Show seed 11/39/41/static irregular windows, normalized `.6` terrain, decks `<=.5`, native feet, direct allowance/service/Retry/checkpoint, both termini, and no Y transform. Capture qualitative screenshots without golden policing. Warm then run signed maximum-knot classifications at exactly 73,094 knots.                                                                                                                                                                                                                                                                                                                                                                                                   |
| Pseudo-can computed-style and screenshot witness                        | For `getComputedStyle(stage,"::after")`, assert `width=20px`, `height=22px`, `pointer-events=none`, `image-rendering=pixelated`, transparent background color, exactly six gradient images, sizes `6px 2px,10px 6px,2px 4px,4px 8px,12px 14px,16px 18px`, positions `6px 2px,4px 0px,16px 10px,16px 8px,2px 6px,0px 4px`, `no-repeat` six times, and alternating normalized paints `rgb(217,74,30)`/`rgb(41,43,48)` in the pinned top-to-bottom order. At DPR 1 and an integer transfer center, take exact `20 by 22` CSS-pixel crops with refueling on and off: on-crop probes `(5,1)`, `(7,3)`, `(18,9)`, `(16,11)`, `(1,5)`, and `(3,7)` prove the six graphite/orange parts; `(0,0)` and `(19,21)` are byte-equal to the off-crop background, proving transparency. The crop visibly reads as one block can. No golden asset ships.                                                                                     |
| Human-authored copy review                                              | A reviewer compares the document title, headings, 404 explanation, controls, outcome/status, action labels, and visible shortcut hints with sections 4 and 13. This is deliberately human evidence; automated suites do not encode authored phrases, substrings, or blacklists.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Manual Chrome and Edge pre-merge; Firefox and Safari/WebKit post-launch | Confirm ordinary windows visibly show materially more frequent/sharper but navigable straight peaks and valleys, meaningful lower/upper band, and no sample chatter, curve, or short rhythm. Confirm terrain can reach `.6` away from pads, accepted decks never exceed `.5`, rejected terrain remains unchanged, each deck is exactly local max `+2.5`, and native supports remain integrated. Confirm fixed scene/no scroll and every unrelated behavior.                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| Responsive, zoom, focus, and accessibility acceptance                   | At exact `320 by 780`, real 400-percent-equivalent `320 by 240`, touch-landscape `667 by 320`, and `1000 by 780`: stage and rail remain normal-flow separated; gauge/outcome, crash/Retry, controls lines, and Exit do not overlap or overflow; buttons are at least `44 by 44`. A real accessibility-tree witness derives expected names from current visible label nodes, proves hint exclusion and shortcut ARIA, one live region, controls IDREF, and tab order shell then Exit or shell then Retry then Exit. Retry returns to shell, Exit to Start; no trap. Authored strings are reviewed by a human, not embedded in automation.                                                                                                                                                                                                                                                                                    |
| Performance and longevity witness                                       | For seeds `11`, `39`, `41`, and `STATIC_WORLD_SEED`, generate 100 sites in both directions and all 4,096 positive sites with no generation failure, covering phases/orders/profiles/ordinals. Direct allowance work is O(1) with no pair key, search, simulation, replay, or catalog. Keep retained state bounded and procedural collision complete; exercise 98,304 streamed segments, tangency, empty-fuel return, final-site/terminus lifecycle, stable listeners/DOM, and existing candidate/collision/frame timing ceilings.                                                                                                                                                                                                                                                                                                                                                                                           |
| Permanent documentation and repository gates                            | `website/README.md` and browser checklist teach the changed actions, ordinary departure, tolerances, rail, accessibility, shared-fragment/no-JS behavior, and derivation workflow in lockstep. Focused suites, deterministic root/project builds, complete gates, file lint, locked-SDD, Rulesync drift, module-size report, and an exact intended-file diff pass. Permanent docs do not link to this SDD.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |

Automated and reviewer acceptance cannot close Phase 4U. After every required check above is green,
the operator must personally play representative seed `11`, seed `41`, and one fresh random run at
wide and exact 320 CSS layouts. The hands-on gate explicitly accepts or rejects: visibly straight
angular terrain; substantial lower/upper band use; broad peaks, valleys, and intermediate facets;
non-sawtooth grade changes; each deck's local height and integrated support scale; fixed scene
height; no vertical follow, page growth, or scroll; and unchanged landing/control feel. Until the
operator records affirmative acceptance, status stays pending and Phase 4U must not merge. A
rejection returns to design; implementation must not answer it by weakening collision, clearance,
landing, terrain/site, or physics bounds.

The 100-site witness asserts each seed's exact indexed superblock profiles and all eight spatial
phases, then proves `M_i=36+192*i`, candidate-order independence from terrain, and byte-identical
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
interpolation, an anchor outside `[.1,.6]`, grade above `.60`, adjacent-grade change above `1.20`, a
forced H/L rhythm or per-sample sawtooth, any shelf/cap/blend/discard splice, terrain access while
deriving a nominal center/order/candidate X, failure to reject `normalizedDeck>.5`, rejection at
exact `.5`, acceptance after the first eligible candidate, more than six candidates, terrain
mutation after rejection, a column rail foot not equal to native interpolation, closed-footprint
maximum drift, a deck not exactly `2.5 m` above that maximum, a global datum, candidate-order
restart after acceptance, profile rejection, a nominal stride other than `192 m`, an accepted
spacing outside `152..232 m`, a complete-run mean outside section 5.3, or either rail displaced from
`+/-786432 m`.

Collision mutations reject procedural terrain sampling that stops at a retained render edge, any
synthetic future/evicted structure descriptor, a finite travel/interval/speed/altitude failure,
`MAX_PLAYABLE_Y`, an absent/invisible/moved terminus, a retained-edge collider, a cutoff-derived
contact, shortest-arc interpolation, right-first traversal, the retired `-24576..24576` clamp, a
vertex/segment endpoint off by one, omission of the segment left of an exact lattice boundary,
allocation proportional to the 98,304-segment domain, or an enclosure/expanded sample treated as
impact. Exact vectors cover high O(1) pruning; low sweeps around `x=-600000` and `x=600000`, each
crossing more than 64 segments and both render edges; the four exact enclosure-index ranges in
section 9; actual crossing, repeated-root stationary tangency, collinear overlap, and near miss;
retained active/target/preceding structure precedence; both physical terminus contacts; empty-fuel
ascent above and return below the former ceiling; and physical angular travel beyond 180 and 360
degrees. They require earliest reconstructed unexpanded contact and unchanged exact-time feature
precedence.

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
or allowance dependence on carried fuel. They reject a second banner/live region, a generated
duplicate status copy, status-wording-driven banner selection, automatic launch, gravity or fuel
burn while launch-ready, steer-only release of the pad hold, discarding the first collective step, a
time-based launch exit, transition before both feet clear, Retry into an already started launch,
duplicated can/fuel/ratio/progress/power, a missing or non-native Retry or Exit control,
hidden-but-enabled actions, wrong active-state Exit projection, or a second launch/status authority.
Phase 4K specifically rejects any `lander-launch` element, controller
lookup/property/listener/handler, fake-DOM member, validator required ID, action-order expectation,
CSS selector, `launch-button` pulse source, or detached/hidden substitute.

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
action in launch-ready or another active state, a visible or tabbable wrong-state action, a target
below `44 by 44`, pointer capture on the rail or an action, missing composed-path/closest
interactive rejection, flight prevention/token/capture/enqueue from an action pointerdown,
`touch-action:none` anywhere except the active stage, non-system or non-monospace fonts, any new
font request/directive/CSP change, generated outcome text, a second status/banner,
`aria-describedby` naming the rail rather than controls prose, a hint included in the accessible
name, missing shortcut ARIA, ambiguous or changed outcome inset/transform rules, or a
320-pixel/400-percent box overlap or horizontal overflow. They also reject other than two direct
controls-line prose children, reversed keyboard/touch order, either line wrapping to multiple client
rectangles, clipping, ellipsis, an inline scrollbar, failure to switch the rail to one column at
`32rem`, Exit sharing the narrow instruction row, or any relevant `scrollWidth>clientWidth`. These
are structural and geometric assertions, not authored-prose assertions. Installed-agent mutations
reject appearance before stage 1, disappearance from a powered retained/checkpoint-restored site, a
new installed-state authority, a body path that does not reproduce the exact four radius-one arcs,
changed terminal/leg subpaths, transform/bounds/paint drift, failure to restore the absent doorway,
a second NOC-entry or child, an absent or duplicate permanent terminus path, any other added world
descendant, a maximum other than exact 76 or greater than 80, and any unexpected geometry digest
change.

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
independently derived v10 ceiling, failure to reproduce the exact strict-X 16 m lattice plus
required boundary/structure insertions, or retained offscreen sites expanding that projection. They
also reject agent travel other than `.9 s`, any change to `.3 s` refuel or `1.4 s` power,
hidden-time advancement, or reduced motion that exposes an intermediate stage.

Input and physics mutations reject component-wise keyboard/pointer engine merging, mixed-input
thrust above straight `1.44`, full-steer total other than `.8`, vector angle other than 30 degrees,
full-steer axial force other than `8.907689867497085`, turn-only axial force other than
`4.175479625389258`, turn-only axial force at or above gravity, pointer override of a nonzero
keyboard steer, canceled keyboard steer blocking an active pointer, an idle or exhausted nonzero
`commanded.vectorAngle`, recording the capture-release association after releasing capture, clearing
its live pulse on synchronous lost capture, ignoring unrelated lost capture, reusable-pointer-ID
matching, an unguarded/duplicate pulse timeout, or accepting a new gesture without atomically
superseding the old pulse. They also reject a second pulse timer, token namespace, or mixer path;
any pulse source in overflow snapshots; any native action that synthesizes thrust; active-shell-path
validation after key classification, Escape, `r`, interactive rejection, or keyup handling;
accepting a stale/foreign shell; or an outside-shell game key that prevents default,
dispatches/focuses an action, or changes held-key/token/queue/pulse/thrust/model state. They reject
interactive-key rejection before in-shell Escape or failed-state `r`; a target-only keyboard guard
that misses a nested composed-path label/hint; or an interactive Space, Enter, arrow, `h`, or `l`
keydown that prevents default, consumes a repeat, changes flight-input state, moves focus, or
dispatches the model. They reject recording or later releasing an interactive-path keydown, failing
to clear accepted input on shell focusout, processing an outside-shell keyup after that teardown,
duplicate native Exit/Retry activation from Space or Enter, or native activation from
arrows/`h`/`l`. They also reject a button pointerdown that prevents default, captures, allocates a
token, or queues a pointer-flight edge; a native click that fires twice after that rejected down;
Exit reaching a stage handler; and ordinary keyboard/vi/pointer/touch departure that discards or
changes the first qualifying fixed step. They retain no passive damping, assist while coasting or
steering, assist that changes total thrust/fuel, reversed/cosmetic-only gimbal, stale 8.4/70
integration, translational mass other than rational `7/10`, force coefficients other than `9/3`,
accelerations other than Number `90/7` and `30/7`, torque other than `80`, or safe-contact limits
other than exact `2.2/3.6/18/26`.

Retry mutations reject moving focus at crash time, any accessible name not derived from the visible
label child, inclusion or hiding of the visible `r` hint, missing `aria-keyshortcuts="r"`, an
out-of-shell `r`, separate click/key recovery paths, focus before the restored render, focus without
`preventScroll`, a retained frame accumulator, held key, queued edge, pointer capture, or pulse, and
any synthesized launch input. They reject checkpoint aliasing, restoring a post-checkpoint flight
pose, fuel, target, progress, ratio, retained can/power/NOC state, or site identity, as well as any
duplicate award, collection, progress, or power transition. Before first success they reject a fresh
seed, a new initial descriptor, or any initial pose/fuel/progress/ratio/window value other than
section 7.3's exact same-run initial approach.

Presentation and allowance mutations reject incomplete static terrain, a rendered gap across
collision geometry, weak scaffold/battery/signal parity, a battery `rx`, terminal path, nub, or
pseudo-element, fewer/more than four bars and three signal paths, an asymmetric signal path,
horizontal/reversed/mistimed stages, color-only meaning, iterative refuel-ratio advancement, an
analytic-real or separately accumulated ratio sum in place of section 5.4's ascending executable
Number loop, or a ratio inconsistent with `refuelRatioForBase(completedSites+1)`. They require one
direct `quantumCeil(22+Math.max(0,targetDeck-originDeck)/3)`, unchanged ratio multiplication, and
additive carryover. They reject a base other than `22`, descent credit, epsilon/integer-key
rounding, a fuel cap, runtime planning/search/simulation/replay, a distance/deck pair key, a
generated route module/import/composition seam, a route fixture/catalog/digest, a route-derived
failure state, terminal delta other than zero, descriptor 4096, or any `smallerFailure` claim.

Geometry mutations reject schema other than v10, any schedule/opening/terminal/proof field in that
fixture, incomplete geometry regeneration, witness seeds other than `[11,39,41,STATIC_WORLD_SEED]`,
wrong signed/full-run nesting, production-derived expected geometry, or a digest payload that omits
or rounds declared geometry authority. Representative flight mutations remove one of the 16 openings
or four section 10.2 classes, change its independently derived geometry, skip exact collision, or
turn the examples into exhaustive/optimal/runtime authority. Closed unsafe collision, unexpanded
target-top handling, transactional initialization, fixed retention, reversible camera motion, crash
debris, ballistic fragments, lifecycle cleanup, privacy, and zero-runtime-network constraints remain
mutation-protected.

Phase 4U projection mutations additionally reject any world Y translation, `--camera-y`, vertical
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
on direct sufficient allowance or geometry digests. Exact mutation vectors pin `n=52,53,54,100` and
prove the 100-site sequence is non-increasing and never below `1`; they do not require it to remain
strictly above `1`.

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
| R30, AC31, Phase 4T: sharper terrain and bounded accepted-deck candidates               | Sections 5.1-5.3, 7, 10, 14.1, and 15               |
| R31, AC32, Phase 4U: final terrain, 192 m pacing, mass, rails, and direct allowance     | Sections 5.1-5.4, 7-10, 14.1, and 15                |

Implementation treats this LLD as temporary design input. Permanent source, tests, and
`website/README.md` stand on their own and do not link back to this SDD path.
