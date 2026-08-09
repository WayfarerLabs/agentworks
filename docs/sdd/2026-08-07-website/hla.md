# HLA: The agentworks.build Website

- Status: Ready for design-PR merge
- Date: 2026-08-07
- Last revised: 2026-08-08
- FRD: `frd.md`
- Research: `prior-art-research.md`
- Brand direction: `brand-direction.md`

## Architectural summary

Build one semantic static page from repository-owned inputs and deploy its artifact to GitHub Pages
on every push to `main`. Site sources live in `website/`; generated output does not. A small
standard-library Python builder performs explicit substitutions, escapes shared text for HTML, emits
the finished artifact, and fails when a required content contract is unavailable or ambiguous.

The main page uses HTML and CSS for its full experience. Small local scripts add copy-button
behavior and the nonessential custom-404 lander game, but canonical and error content remain useful
when scripts are absent. The visual system includes local SVG logo assets. There are no remote
fonts, scripts, images, runtime APIs, analytics, cookies, accounts, forms, or backend.

GitHub Actions builds and checks the same artifact in pull requests and on `main`. A dedicated Pages
workflow uploads that artifact and deploys it through the protected `github-pages` environment.
GitHub Pages serves `agentworks.build` over HTTPS; `www.agentworks.build` redirects to the apex.

## Decisions

### D1. One page, not a small documentation site

The first slice is one page with this information order:

1. a compact identity and problem statement;
2. the agent-addressed bootstrap as the dominant action;
3. a concise explanation of the operating model and principles;
4. direct GitHub, PyPI, deeper rationale, and security links.

The page has in-page navigation only if the final content length makes it useful. A custom
`404.html` is an error surface, not a second content page or client-side route. There is no blog,
documentation hierarchy, release feed, search, or client-side routing. Growth-path content gets its
own design when its authoritative contracts have landed.

### D2. Plain web technologies with a narrow build step

The checked-in source consists of main-page and 404 HTML templates, local CSS, focused
progressive-enhancement JavaScript, SVG assets, and a standard-library Python builder under
`website/`. There is no separate build-metadata abstraction. The builder's explicit input list is
the build manifest. It uses the supported Python runtime already present in this repository and only
the standard library. It does not introduce Node package metadata, a JavaScript framework, Jekyll,
or a general template language.

The builder accepts an explicit repository root and output directory, writes only beneath the output
directory, and produces deterministic bytes for the same inputs. It starts from an empty
caller-provided output directory in CI. Generated artifacts are ignored by Git and never committed.

The template vocabulary is closed to named placeholders owned by the builder. Shared text is HTML
escaped before insertion. There is no evaluation of source content as a template, Markdown, Python,
or JavaScript.

### D3. Repository content is a checked contract

The page has three content classes:

- **Canonical shared content.** After onboarding Phase 3 lands on `main`, the build reads its
  canonical bootstrap body directly. The built code element's decoded text, the canonical source,
  and the README's generated fenced block must be byte-identical. The build fails closed on missing,
  duplicate, malformed, or drifted sources. The website does not own or rewrite this text.
- **Repository-derived product content.** The problem statement and principle passages are selected
  by a unique Markdown heading and exact expected paragraph text from permanent repository docs,
  never by paragraph position. The matching passage is normalized by a closed paragraph transform,
  HTML escaped, and generated into the page. The template cannot supply alternate product copy.
  Missing headings, duplicate headings, absent or duplicate expected text, or unsupported Markdown
  fail the build. Unrelated paragraph insertion or reordering does not. Links point to permanent
  repository docs, never to this SDD.
- **Site-owned connective content.** The website owns only presentation-neutral labels and
  instructions such as navigation, "Copy", link introductions, and copy-status feedback. It does not
  make claims about Agentworks behavior, guarantees, principles, installation, or requirements.

The exact upstream bootstrap path, extraction contract, README fence semantics, and permanent
paragraph text are deliberately confirmed and pinned in the site LLD against `main`. README
fence-body byte equality is an assumption until that pickup proves the merged onboarding contract. A
branch-only path is not an architecture input. Implementation cannot begin on the bootstrap
integration until the canonical source is on `main`.

### D4. HTML is the agent surface too

The same document serves humans, agents, assistive technology, text browsers, and indexing tools.
The built page provides:

- one descriptive `title`, meta description, canonical URL, and ordinary indexable markup;
- semantic landmarks and one correctly nested heading outline;
- real anchor and button elements with accessible names;
- the complete bootstrap in a `pre`/`code` region, without image-baked or CSS-generated text;
- useful link text that identifies GitHub, PyPI, rationale, and security destinations;
- no essential state hidden behind interaction, animation, canvas, or client rendering.

No `llms.txt` or second agent-only representation is introduced. It would be another content copy
without a settled standard or first-slice need.

### D5. Visual system is local, restrained, and distinctive

The first slice uses system fonts, a small local color/token layer, and the selected custom AGW
rocket mark. The mark stacks custom symmetric A, G, and W geometry into a rocket silhouette; the G
has four equal corner radii, with only its opening and inward stroke breaking O-like symmetry. The
ordinary mark is neutral graphite. Its original twin-flame treatment uses compact pale-yellow hot
cores within orange and deeper orange-red plumes. The final checked-in SVG is self-contained,
font-independent, semantic where displayed as content, and reusable without this SDD.

The presentation should feel like a capable workbench rather than a generic SaaS landing page:
strong typography, restrained color, visible structure, and the bootstrap block as the visual
center. No remote font, icon library, or existing architecture diagram is introduced.

The site LLD pins final tokens and layouts with these invariants:

- useful at 320 CSS pixels and at 400 percent zoom without page-level horizontal scrolling;
- WCAG 2.2 AA text, component, focus, and interaction contrast;
- visible keyboard focus and a logical tab order;
- no required motion, and any decorative motion disabled under `prefers-reduced-motion`;
- light and dark presentation only if both can be tested to the same bar. A single excellent theme
  is preferred to two partially verified ones.

### D6. Copy is progressive enhancement

The bootstrap text is selectable and readable before JavaScript runs. The copy button reads the code
element's `textContent`, invokes the Clipboard API only from the user's activation, and reports
success or failure in an `aria-live` status region without moving focus. If the API is unavailable,
the button is absent or explains that manual selection remains available. No clipboard content is
read.

### D7. The custom 404 hides a bounded deployment game

The built artifact includes a semantic `404.html` that identifies the missing page and exposes a
normal link home without CSS or JavaScript. The selected twin-plume mark hovers over a minimal lunar
surface. No visual instructions, score, or game chrome appear initially. On arrival, the plumes run
a subtle cue for less than five seconds and settle; `prefers-reduced-motion: reduce` suppresses that
cue entirely. This bounded cue preserves the surprise without requiring a pre-game pause control.

An unmodified, non-repeated Space key starts the game from the initial 404 state when its event
target is the document body or lander scene, never when focus is on the home link or another
interactive/editable element. The lander is also an operable, accessibly named start control without
visible instruction text. Activating it provides the pointer and assistive-technology path. The
accepted preflight Space event is consumed so it cannot also scroll the page. Starting moves focus
to the game scene and reveals concise controls and status.

While active, Space or Up commands equal thrust; Left or `h` increases the right engine to turn
left; Right or `l` increases the left engine to turn right. Apart from the accepted preflight Space
event, only active game controls prevent their ordinary browser behavior. A first pointer activation
starts without also applying thrust. Thereafter, pointer down captures that pointer and starts
collective thrust; movement applies a bounded horizontal differential after a dead zone; pointer up,
cancel, lost capture, exit, or page hide cuts both engines and releases capture. A short press with
little travel receives a pinned minimum impulse so a tap is useful; holding sustains thrust.
Dragging left biases the right engine, and dragging right biases the left. `touch-action` and scroll
suppression apply only inside the active game scene. Escape exits to the settled initial state, and
`r` restarts a completed or failed mission.

The game is a small DOM/SVG state machine, not canvas and not a general engine. A timestamp-driven
animation loop integrates a fixed-step two-dimensional model with bounded catch-up: gravity,
position, velocity, attitude, angular velocity, collective thrust, differential torque, fuel, and
landing contact. The loop pauses when the document is hidden and resumes without accumulating hidden
time. Within the pinned catch-up bound, the same timestamped input timeline produces equivalent
fixed-step simulation across representative 30, 60, and 120 Hz frame schedules. A larger stall
discards accumulated wall time and resumes from the last state rather than pretending to simulate
unseen play. Each plume group scales independently from the commanded engine thrust.

A valid landing requires contact within the marked zone, left of a small dark NOC cluster, under
pinned speed and attitude limits. After touchdown, the G opening acts as a deployment bay: a small
terminal-inspired agent reaches the surface and enters the NOC. Its windows and server-status lights
illuminate in sequence, followed by a restrained antenna signal. The powered appearance remains for
the rest of the in-memory run while the lander lifts away. The accessible status then becomes
exactly `Agent deployed. Mission continues.` Reduced motion skips the decorative travel and
departure and presents the powered NOC and status directly. An unsafe contact produces a restrained
failure state without flashing, sound, debris, or page damage. The home link remains available in
every state.

Game constants, DOM states, collision rules, agent-deployment geometry, and test vectors belong in
`brand-and-lander-lld.md`. The game makes no product claim and carries no analytics, storage,
network request, randomness that affects acceptance, or critical content. Restart or reload returns
the NOC to its initial dark state; there is no cross-run persistence or level system.

### D8. GitHub Pages is the replaceable delivery adapter

The existing CI workflow builds and tests the site on pull requests and pushes to `main`, without
Pages permissions. Before the implementation PR merges, the operator enables GitHub Actions as this
repository's Pages source and restricts the `github-pages` environment to the default branch. The
publishing workflow then runs only after a push to `main` and has two jobs:

1. **Build:** check out the repository, run the website contract tests and builder, verify the
   artifact, configure Pages metadata, and upload exactly the generated directory.
2. **Deploy:** on `main` only, deploy the uploaded artifact to the `github-pages` environment with
   the minimum `pages: write` and `id-token: write` permissions.

The publishing build repeats the deterministic checks before it uploads, so deployment never trusts
an artifact from a different event. Deployment concurrency allows one active publish and cancels
superseded queued work without cancelling an in-progress production deployment.

The publishing workflow is not path-filtered. The site's authoritative inputs include files outside
`website/`, so every push to `main` must rebuild and verify the artifact rather than risk serving a
stale bootstrap or product passage.

The workflow pins current stable major versions of official GitHub actions at implementation time,
consistent with repository conventions. No third-party deploy action or long-lived cloud credential
is introduced.

The artifact boundary is portable: another static host can accept the generated directory without
changing content or build contracts. Hosting-specific behavior stays in the workflow and operator
runbook.

### D9. DNS and domain setup are explicit one-time operations

The permanent `website/README.md` runbook records external setup, current DNS inventory, approval,
rollback, and recovery. Setup and go-live are ordered so deployment exists before DNS cutover:

1. enable GitHub Actions as this repository's Pages source and protect the `github-pages`
   environment so only `main` can deploy;
2. merge the implementation and verify its automatic deployment at the default Pages URL;
3. verify `agentworks.build` in the WayfarerLabs GitHub organization and retain GitHub's TXT record;
4. set `agentworks.build` as this repository's custom domain;
5. after explicit operator approval, replace GoDaddy's apex parking record with GitHub's documented
   `A` records and point `www` by `CNAME` to `wayfarerlabs.github.io`;
6. verify DNS answers, apex content, `www` redirect, certificate, and HTTPS enforcement;
7. remove or repoint the DNS records promptly if Pages is ever disabled.

DNS values are copied from current GitHub documentation during go-live and recorded in acceptance
evidence. They are not hidden in application code. No wildcard record is created.

## Component topology

```text
canonical onboarding bootstrap (after merge) ----+
                                                  |
README generated fenced block -------------------+--> content contract checks
                                                  |
README + docs/why-agentworks.md paragraph selectors
                                                  |
                                                  +--> generated product passages
                                                  |
website HTML template + local CSS/JS -------------+--> deterministic builder
                                                           |
website 404 template + logo/game CSS/JS/SVG ------+        |
                                                           v
                                              static site artifact (index + 404)
                                                           |
                                      +--------------------+------------------+
                                      |                                       |
                                      v                                       v
                              PR/CI verification                    GitHub Pages deploy
                                                                              |
                                                                              v
                                                   agentworks.build (canonical apex)
                                                                              |
                                                   www.agentworks.build redirect
```

## Source layout

The detailed filenames belong in the site LLD, but responsibilities are fixed here:

- `website/`: page and 404 source, final SVG assets, focused CSS/JavaScript, builder, tests, and
  permanent operator/developer runbook.
- `.github/workflows/`: Pages build/deploy workflow and the existing CI integration.
- `.gitignore`: generated site artifact exclusion.
- repository README and onboarding canonical source: inputs, not website-owned copies.
- `docs/why-agentworks.md`: permanent deeper rationale linked and checked as a claim source.
- this feature directory: temporary design, plan, research, and acceptance evidence only.

## Verification strategy

### Build and content contracts

- deterministic clean build in a temporary directory;
- no unexpanded placeholders or writes outside the requested output;
- exact bootstrap equality across canonical source, README fenced block, and decoded built HTML;
- unique source headings and paragraph selectors, closed normalization, and required product links;
- valid internal paths, canonical URL, metadata, and no external runtime assets;
- generated output absent from Git status.

### Document behavior

- semantic landmarks, heading order, language, named controls, and bootstrap code region asserted;
- copy behavior exercised for success, unavailable API, and rejected write without changing the
  source text;
- custom 404 fallback, initial hidden controls, bounded idle cue, keyboard/vi/pointer mappings,
  deterministic physics vectors, plume-to-thrust mapping, success/failure/restart/exit states,
  background pause, and agent-deployment completion asserted;
- keyboard-only traversal, visible focus, narrow-width reflow, zoom, reduced motion, and screen
  reader landmarks checked in acceptance;
- color tokens verified with computed contrast evidence for normal text, large text, components, and
  focus indicators.

### Deployment and production

- pull request proves build without deploy permissions;
- a merge-to-`main` source change produces a successful Pages deployment with the expected commit;
- production returns the built content over HTTPS at the apex;
- `www` redirects to the apex without a certificate warning;
- DNS A, AAAA, CNAME, MX, TXT, and CAA answers match the recorded before-state plus approved cutover
  delta;
- GitHub and PyPI links resolve, and the copied production bootstrap remains byte-identical.

## Security and privacy

- The site adds no first-party telemetry, user storage, analytics, cookies, forms, third-party
  script, remote font, or runtime network request. GitHub retains its ordinary hosting and
  operational processing.
- Shared text is escaped before HTML insertion. The builder never evaluates it.
- Game state is ephemeral in memory and accepts no text, URL, storage, or network input. Pointer and
  keyboard handlers are scoped to the active scene and released on exit or page lifecycle changes.
- A restrictive document policy is expressed where GitHub Pages allows it without breaking the page;
  lack of configurable response headers is documented as a Pages constraint, not simulated as a
  guarantee.
- The deployment workflow grants read-only contents plus job-local Pages/OIDC permissions and uses
  no repository or cloud secret.
- Organization domain verification and no-wildcard DNS reduce custom-domain takeover risk.
- External links do not receive opener authority if a new browsing context is used.

## Failure and recovery

- **Upstream content drift:** build fails before artifact upload and names the missing or mismatched
  contract. The owner updates the website integration against the merged source; it never guesses.
- **JavaScript unavailable or game failure:** the semantic 404 message and ordinary home link remain
  available. The lander is nonessential and never owns navigation or recovery.
- **Bad site merge:** the Pages environment exposes deployment history. Fix forward or redeploy the
  last known-good artifact according to the runbook.
- **DNS or certificate delay:** the Pages deployment history, status, and captured pre-attachment
  default-URL acceptance remain the diagnostics. Do not weaken HTTPS or add alternate forwarding
  machinery while propagation is incomplete.
- **Pages disabled or repository moved:** remove the public DNS records immediately if the domain is
  no longer attached, then follow the runbook to restore verified ownership before repointing.
- **Hosting migration:** deploy the same generated artifact elsewhere, validate it at a temporary
  hostname, then move DNS. Site sources and content contracts stay unchanged.

## Deliberately deferred growth paths

- Web rendering of `agw guide` topics.
- Schema-derived reference pages.
- Release notes, changelog presentation, search, and multiple-page navigation.
- Analytics, feedback capture, accounts, forms, APIs, and dynamic backends.
- A general component system, design system package, or static-site framework.

Each requires a new requirement or a stable upstream contract. None is scaffolded in this slice.
