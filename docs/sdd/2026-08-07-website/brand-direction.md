# Brand Direction: AGW Rocket

- Status: Selected for implementation
- Date: 2026-08-08
- Decision owner: operator

## Selected mark

The selected mark is the custom symmetric soft-stack AGW rocket with the original twin layered
plumes, captured in `logo-concept-10-twin-flame.svg`.

- The A is the nosecone.
- The G is the body. Its four outer corners share one radius; only the right-side opening and inward
  stroke break the symmetry of an O.
- The W is the two-engine assembly.
- The A, G, and W retain shallow mirrored overlaps so the mark is one connected silhouette while
  preserving a small amount of negative space below the A crossbar.
- The letters use neutral graphite. Each engine uses the same nested pale-yellow, orange, and deep
  orange-red plume layers.
- The selected twin plume is intentionally a little wider than the bottom elbow above it. The
  aligned twin-plume exploration was considered and rejected.

The geometry is custom and font-independent. JetBrains Mono remains a favored direction for the
eventual wordmark and terminal-inspired site typography, but no remote or bundled font is required
by the first slice.

## Interactive behavior

The custom 404 page treats the mark as a lunar deployment vehicle.

- Before activation, the lander has no visual game instructions. The twin plumes provide a brief,
  subtle cue and then settle. Reduced-motion presentation is static.
- Space starts keyboard play. Activating the lander starts touch or assistive-technology play.
- Space or Up commands equal thrust. Left or `h` increases the right engine to turn left. Right or
  `l` increases the left engine to turn right.
- Tap produces a short equal-thrust pulse; press-and-hold sustains it; horizontal drag while pressed
  biases the opposite engine and turns in the drag direction.
- Plume length follows each engine's commanded thrust independently.
- The landing zone sits left of a small, initially dark NOC cluster. After a safe landing, the G
  opening becomes a deployment bay. A small terminal-inspired agent reaches the surface, enters the
  NOC, and powers it up. Windows, server-status lights, and a restrained antenna signal remain
  active for the rest of the run while the lander departs. The experience concludes with
  `Agent deployed. Mission continues.`

The 404 remains a useful static error page with a normal home link in every state. The game deploys
only with the complete website. The powered NOC is session-local game state; reload or restart
begins a fresh mission without cookies or browser storage.

## Promotion rule

This file and the numbered concepts are temporary design history. Implementation copies the selected
geometry and behavior into permanent, self-contained files under `website/`; permanent code and
documentation must not depend on this SDD path.
