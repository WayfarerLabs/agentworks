# ruff: noqa: F405

import xml.etree.ElementTree as ET

from site_test_support import *  # noqa: F403


def transformed_origin(
    transform: str,
    origin: tuple[float, float],
    replacements: dict[str, str],
) -> tuple[float, float]:
    """Execute the SVG transform list against its CSS transform origin."""
    for variable, value in replacements.items():
        transform = transform.replace(f"var({variable})", value)
    point = [0.0, 0.0]
    functions = re.findall(r"([a-z]+)\(([^)]*)\)", transform)
    for name, source in reversed(functions):
        values = [float(value.removesuffix("px").removesuffix("deg")) for value in re.split(r"[,\s]+", source.strip())]
        if name == "translate":
            point[0] += values[0]
            point[1] += values[1] if len(values) > 1 else 0
        elif name == "scale":
            point[0] *= values[0]
            point[1] *= values[1] if len(values) > 1 else values[0]
        elif name == "rotate":
            radians = math.radians(values[0])
            point = [
                point[0] * math.cos(radians) - point[1] * math.sin(radians),
                point[0] * math.sin(radians) + point[1] * math.cos(radians),
            ]
        else:
            raise AssertionError(f"unsupported transform function: {name}")
    return origin[0] + point[0], origin[1] + point[1]


GLOBAL_OBJECT = r"(?:\bwindow\b|\bglobalThis\b)"


def member_access(object_pattern: str, member: str) -> str:
    """Return a pattern for dot or bracket access to one JavaScript member."""
    return rf"(?:{object_pattern})\s*(?:\.\s*{member}\b|\[\s*['\"]{member}['\"]\s*\])"


NAVIGATOR_OBJECT = rf"(?:\bnavigator\b|{member_access(GLOBAL_OBJECT, 'navigator')})"
DOCUMENT_OBJECT = rf"(?:\bdocument\b|{member_access(GLOBAL_OBJECT, 'document')})"
LOCATION_OBJECT = rf"(?:\blocation\b|{member_access(GLOBAL_OBJECT, 'location')})"
HISTORY_OBJECT = rf"(?:\bhistory\b|{member_access(GLOBAL_OBJECT, 'history')})"

FORBIDDEN_RUNTIME_PATTERNS = {
    "fetch": rf"(?:\bfetch\s*\(|{member_access(GLOBAL_OBJECT, 'fetch')})",
    "XMLHttpRequest": r"\bXMLHttpRequest\b",
    "WebSocket": r"\bWebSocket\b",
    "EventSource": r"\bEventSource\b",
    "sendBeacon": member_access(NAVIGATOR_OBJECT, "sendBeacon"),
    "cookie": member_access(DOCUMENT_OBJECT, "cookie"),
    "cache": r"\b(?:Cache|CacheStorage|caches)\b",
    "service worker": (rf"\bServiceWorker\b|{member_access(NAVIGATOR_OBJECT, 'serviceWorker')}"),
    "location navigation": (rf"{member_access(LOCATION_OBJECT, '(?:href|assign|replace)')}"),
    "history mutation": (rf"{member_access(HISTORY_OBJECT, '(?:pushState|replaceState)')}"),
    "storage": r"\b(?:localStorage|sessionStorage|indexedDB)\b",
}

FORBIDDEN_RUNTIME_CANARIES = {
    "fetch": (
        'window["fetch"]("/collect")',
        "const request = window.fetch; request('/collect')",
    ),
    "XMLHttpRequest": ('window["XMLHttpRequest"]',),
    "WebSocket": ('window["WebSocket"]',),
    "EventSource": ('window["EventSource"]',),
    "sendBeacon": (
        'navigator["sendBeacon"]("/collect")',
        'window["navigator"]["sendBeacon"]("/collect")',
    ),
    "cookie": ('document["cookie"]', 'window["document"]["cookie"]'),
    "cache": ('window["caches"]', 'window["CacheStorage"]'),
    "service worker": (
        'navigator["serviceWorker"]',
        'window["navigator"]["serviceWorker"]',
    ),
    "location navigation": (
        'location["assign"]("/")',
        'window["location"]["href"]',
    ),
    "history mutation": (
        'history["pushState"]({}, "", "/")',
        'window["history"]["pushState"]({}, "", "/")',
    ),
    "storage": (
        'window["localStorage"]',
        'window["sessionStorage"]',
        'window["indexedDB"]',
    ),
}


class SiteBaseTests(unittest.TestCase):
    def test_root_and_project_bases_pass(self) -> None:
        self.assertEqual(site_builder.validate_site_base("/"), "/")
        self.assertEqual(site_builder.validate_site_base("/agentworks/"), "/agentworks/")
        self.assertEqual(site_builder.validate_site_base("/agent-works_1.0~/"), "/agent-works_1.0~/")

    def test_invalid_bases_are_rejected(self) -> None:
        invalid = (
            "",
            "agentworks/",
            "/agentworks",
            "https://agentworks.build/",
            "//agentworks.build/",
            "/agentworks/?mode=test",
            "/agentworks/#fragment",
            "/agentworks\\demo/",
            "/agentworks//demo/",
            "/agentworks/./demo/",
            "/agentworks/../demo/",
            "/agentworks/%2e%2e/demo/",
            "/agentworks/%2Fdemo/",
            '/agentworks/"/',
            "/agentworks/'/",
            "/agentworks/</",
            "/agentworks/>/",
            "/agentworks/\n/",
            "/agentworks/\t/",
            "/agentworks/\x00/",
            "/agentworks/%22/",
            "/agentworks/%27/",
            "/agentworks/%3C/",
            "/agentworks/%0A/",
            "/agentworks/%20/",
            "/agentworks/{value}/",
            "/agentworks/&value/",
            "/agéntworks/",
            " /agentworks/",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                site_builder.validate_site_base(value)


class BuildTests(unittest.TestCase):
    expected = EXPECTED_FILES

    def build(self, site_base: str) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
        temporary = tempfile.TemporaryDirectory()
        output = Path(temporary.name) / "site"
        site_builder.build_site(REPO_ROOT, output, site_base)
        return output, temporary

    def test_root_and_project_builds_have_the_exact_output_set(self) -> None:
        for site_base in ("/", "/agentworks/"):
            with self.subTest(site_base=site_base):
                output, temporary = self.build(site_base)
                self.addCleanup(temporary.cleanup)
                files = {path.relative_to(output) for path in output.rglob("*") if path.is_file()}
                self.assertEqual(files, self.expected)
                self.assertEqual(site_builder.FULL_MANIFEST, self.expected)
                for route in (Path("404.html"), Path("lander/index.html")):
                    html = (output / route).read_text(encoding="utf-8")
                    self.assertNotIn("{{", html)
                    self.assertIn(f'href="{site_base}"', html)
                    self.assertIn(f'href="{site_base}static/lander.css"', html)
                    self.assertIn(f'href="{site_base}static/site.css"', html)
                    self.assertIn(f'src="{site_base}static/lander-game.js"', html)
                    for fragment in ("agw-mark", "agw-engine-left", "agw-engine-right"):
                        self.assertIn(f'href="{site_base}assets/agw-rocket.svg#{fragment}"', html)

    def test_builder_replaces_only_an_owned_output_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "site"
            site_builder.build_site(REPO_ROOT, output, "/")
            site_builder.build_site(REPO_ROOT, output, "/agentworks/")
            (output / "unrelated.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(ValueError):
                site_builder.build_site(REPO_ROOT, output, "/")
            self.assertEqual((output / "unrelated.txt").read_text(encoding="utf-8"), "keep")

    def test_builder_rejects_every_repository_output_before_writing(self) -> None:
        protected = (
            WEBSITE / "templates" / "404.html",
            WEBSITE / "assets" / "agw-rocket.svg",
            WEBSITE / "static" / "lander.css",
            WEBSITE / "static" / "lander-model.js",
            WEBSITE / "static" / "lander-game.js",
        )
        before = {path: path.read_bytes() for path in protected}
        staging_before = set(REPO_ROOT.rglob("agentworks-404-*"))
        targets = (
            REPO_ROOT,
            WEBSITE,
            WEBSITE / "templates",
            WEBSITE / "static",
            WEBSITE / "templates" / "404.html",
            WEBSITE / "templates" / "nested-output",
            WEBSITE / "static" / "nested-output",
        )
        for target in targets:
            with self.subTest(target=target), self.assertRaises(ValueError):
                site_builder.build_site(REPO_ROOT, target, "/")
        self.assertEqual({path: path.read_bytes() for path in protected}, before)
        self.assertEqual(set(REPO_ROOT.rglob("agentworks-404-*")), staging_before)
        self.assertFalse((WEBSITE / "templates" / "nested-output").exists())
        self.assertFalse((WEBSITE / "static" / "nested-output").exists())

    def test_closed_template_vocabulary_and_required_references_fail_closed(
        self,
    ) -> None:
        template = (WEBSITE / "templates" / "404.html").read_text(encoding="utf-8")
        self.assertEqual(
            set(re.findall(r"{{[^{}]+}}", template)),
            {"{{SITE_BASE}}", "{{LANDER_GAME}}"},
        )
        with self.assertRaises(ValueError):
            site_builder._validate_template("404.html", template + "{{OTHER}}")
        for required in site_builder.REQUIRED_404_REFERENCES:
            with self.subTest(required=required), self.assertRaises(ValueError):
                site_builder._validate_template("404.html", template.replace(required, "missing"))


class StaticDocumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        shell = (WEBSITE / "templates" / "404.html").read_text(encoding="utf-8")
        cls.fragment = (WEBSITE / "templates" / "lander-game.html").read_text(encoding="utf-8")
        cls.template = shell.replace("{{LANDER_GAME}}", cls.fragment)
        cls.document = parse(cls.template)
        cls.css = (WEBSITE / "static" / "lander.css").read_text(encoding="utf-8")
        cls.model = (WEBSITE / "static" / "lander-model.js").read_text(encoding="utf-8")
        cls.game = (WEBSITE / "static" / "lander-game.js").read_text(encoding="utf-8")

    def element(self, element_id: str) -> tuple[str, dict[str, str | None]]:
        return next(item for item in self.document.start_tags if item[1].get("id") == element_id)

    def test_no_javascript_document_is_a_useful_semantic_404(self) -> None:
        tags = [tag for tag, _ in self.document.start_tags]
        self.assertEqual(tags.count("header"), 1)
        self.assertEqual(tags.count("main"), 1)
        self.assertEqual(tags.count("footer"), 1)
        self.assertEqual(tags.count("h1"), 1)
        home_links = [attributes for tag, attributes in self.document.start_tags if attributes.get("href") == "{{SITE_BASE}}"]
        self.assertEqual(len(home_links), 1)
        self.assertNotIn("hidden", self.element("not-found-message")[1])

    def test_preflight_controls_are_hidden_but_scene_and_breadcrumb_are_not(
        self,
    ) -> None:
        self.assertIn("hidden", self.element("lander-start")[1])
        self.assertIn("hidden", self.element("lander-controls-rail")[1])
        self.assertIn("hidden", self.element("lander-outcome")[1])
        self.assertIn("hidden", self.element("lander-restart")[1])
        self.assertNotIn("hidden", self.element("lander-scene")[1])
        home = next(attributes for tag, attributes in self.document.start_tags if attributes.get("href") == "{{SITE_BASE}}")
        self.assertNotIn("hidden", home)

    def test_accessible_names_live_region_and_initial_focus_surface_are_pinned(
        self,
    ) -> None:
        self.assertEqual(len(self.document.ids), len(set(self.document.ids)))
        self.assertTrue(" ".join((self.element("lander-game")[1]["aria-label"] or "").split()))
        self.assertTrue(self.element("lander-start")[1]["aria-label"])
        self.assertEqual(self.element("lander-scene-shell")[1]["tabindex"], "-1")
        status = self.element("lander-status")[1]
        self.assertEqual(status["role"], "status")
        self.assertEqual(status["aria-live"], "polite")
        self.assertEqual(status["aria-atomic"], "true")
        self.assertEqual(self.element("lander-exit")[0], "button")
        self.assertEqual(self.element("lander-restart")[0], "button")
        self.assertEqual(self.element("lander-fuel-gauge")[1]["aria-hidden"], "true")
        self.assertEqual(self.element("lander-fuel-value")[0], "span")
        self.assertEqual(self.element("lander-fuel-label")[1]["class"], "visually-hidden")
        self.assertEqual(self.element("lander-fuel-value")[1]["class"], "visually-hidden")
        self.assertNotIn("role", self.element("lander-fuel-value")[1])
        self.assertNotIn("aria-live", self.element("lander-fuel-value")[1])
        self.assertNotIn("aria-labelledby", self.element("lander-fuel-value")[1])
        self.assertNotIn("output", [tag for tag, _ in self.document.start_tags])
        self.assertTrue(" ".join(self.document.text_by_id["lander-scene-description"].split()))

    def test_scene_geometry_and_start_target_are_fixed_and_responsive(self) -> None:
        scene = self.element("lander-scene")[1]
        self.assertEqual(scene["viewbox"], "0 0 1000 640")
        self.assertEqual(scene["preserveaspectratio"], "xMidYMid meet")
        self.assertIn("aspect-ratio: 25 / 16", self.css)
        self.assertIn("width: min(100%, 60rem)", self.css)
        shell_rule = self.css.split("#lander-scene-shell {", 1)[1].split("}", 1)[0]
        stage_rule = self.css.split("#lander-scene-stage {", 1)[1].split("}", 1)[0]
        self.assertNotRegex(shell_rule, r"min-width\s*:")
        self.assertNotIn("aspect-ratio", shell_rule)
        self.assertIn("aspect-ratio: 25 / 16", stage_rule)
        self.assertIn("top: 31.7625%", self.css)
        self.assertIn("left: 30%", self.css)
        self.assertIn("width: max(44px, 2.816%)", self.css)
        self.assertIn("height: max(44px, 12.525%)", self.css)
        self.assertIn("#lander-start:focus-visible", self.css)
        self.assertIn("transform-origin: 82px 401px", self.css)
        self.assertIn("transform-origin: 158px 401px", self.css)
        terrain = [
            attributes
            for _, attributes in self.document.start_tags
            if attributes.get("class") in {"terrain-fill", "terrain-surface"}
        ]
        self.assertEqual([attributes["class"] for attributes in terrain], ["terrain-fill", "terrain-surface"])
        self.assertEqual(terrain[0]["stroke"], "none")
        self.assertTrue(terrain[0]["d"].endswith("Z"))
        self.assertEqual(terrain[1]["fill"], "none")
        self.assertNotRegex(terrain[1]["d"], r"[VZ]")
        surface = [
            (float(x), float(y)) for x, y in re.findall(r"[ML](-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)", terrain[1]["d"])
        ]
        self.assertEqual((surface[0][0], surface[-1][0]), (0, 1000))
        self.assertIn((312, 478.72), surface)
        self.assertIn((322, 481.2), surface)
        self.assertIn((400, 528), surface)
        self.assertIn((410, 534), surface)
        self.assertIn((488, 571.2), surface)
        self.assertIn((498, 565.2), surface)
        deck = next(attributes for _, attributes in self.document.start_tags if attributes.get("class") == "landing-platform")
        self.assertEqual(float(deck["y"]), 453.72)
        self.assertEqual(float(deck["y"]) + float(deck["height"]), 457.21999999999997)
        support = next(attributes for _, attributes in self.document.start_tags if attributes.get("class") == "site-scaffold")
        self.assertTrue(
            support["d"].startswith("M312 457.21999999999997H498M312 464.71999999999997H498")
        )
        self.assertEqual(support["d"].count("M"), 75)
        self.assertNotIn("Z", support["d"])
        self.assertEqual(support["fill"], "none")
        self.assertEqual(support["stroke-linecap"], "butt")
        self.assertEqual(support["stroke-linejoin"], "round")
        noc = next(attributes for _, attributes in self.document.start_tags if attributes.get("class") == "noc-building")
        self.assertTrue(noc["d"].startswith("M428 457.21999999999997V"))
        self.assertIn("rotate(var(--thrust-vector-angle))", self.css)

    def test_css_has_only_bounded_keyframes_and_reduced_motion_preserves_live_plumes(
        self,
    ) -> None:
        self.assertEqual(
            set(re.findall(r"@keyframes\s+([\w-]+)", self.css)),
            {"agw-preflight-cue", "agw-target-cue", "agw-fuel-empty-blink"},
        )
        empty_gauge = self.css.split('[data-fuel-level="empty"] #lander-fuel-gauge {', 1)[1].split("}", 1)[0]
        self.assertIn("animation: agw-fuel-empty-blink 700ms steps(1, end) infinite", empty_gauge)
        self.assertIn("background: var(--fuel-danger-color)", empty_gauge)
        reduced = self.css.split("@media (prefers-reduced-motion: reduce)", 1)[1]
        self.assertIn("animation: none !important", reduced)
        self.assertIn("transition: none !important", reduced)
        self.assertNotIn("#mission-left-engine", reduced)
        self.assertNotIn("#mission-right-engine", reduced)
        self.assertNotIn("scale(1, 0.08)", reduced)
        self.assertIn('[data-paused="true"]', self.css)
        powered_rule = self.css.split('.lander-site[data-noc-stage="7"] .antenna-signal-3 {', 1)[1].split("}", 1)[0]
        self.assertIn("opacity: 1", powered_rule)
        self.assertNotIn("animation", powered_rule)

    def test_engine_transforms_keep_both_nozzle_anchors_fixed(self) -> None:
        engines = {
            "#mission-left-engine": ((82.0, 401.0), "--left-plume-scale"),
            "#mission-right-engine": ((158.0, 401.0), "--right-plume-scale"),
        }
        for selector, (origin, scale_variable) in engines.items():
            block = re.search(
                rf"{re.escape(selector)}\s*\{{([^}}]+)\}}",
                self.css,
                re.DOTALL,
            )
            self.assertIsNotNone(block)
            transform = re.search(r"transform:\s*([^;]+);", block[1], re.DOTALL)
            declared_origin = re.search(
                r"transform-origin:\s*([\d.]+)px\s+([\d.]+)px",
                block[1],
            )
            self.assertIsNotNone(transform)
            self.assertIsNotNone(declared_origin)
            self.assertEqual(tuple(map(float, declared_origin.groups())), origin)
            for angle in (-18, 18):
                for scale in (0.08, 0.5, 1):
                    actual = transformed_origin(
                        transform[1],
                        origin,
                        {
                            "--thrust-vector-angle": f"{angle}deg",
                            scale_variable: str(scale),
                        },
                    )
                    self.assertAlmostEqual(actual[0], origin[0])
                    self.assertAlmostEqual(actual[1], origin[1])
        cue = self.css.split("@keyframes agw-preflight-cue {", 1)[1].split("@keyframes agw-target-cue", 1)[0]
        for transform in re.findall(r"transform:\s*([^;]+);", cue):
            for origin, _ in engines.values():
                self.assertEqual(transformed_origin(transform, origin, {}), origin)

    def test_input_clear_restores_zero_command_and_renders(self) -> None:
        clear_input = self.game.split("clearAllInput(timestamp, quiesce = false) {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("commanded: { ...ZERO_INPUT }", clear_input)
        self.assertIn("vectorAngle: 0", self.game)
        self.assertIn("clearSimulationInput", clear_input)
        frame = self.game.split("frame(timestamp) {", 1)[1].split("reconcileWorld() {", 1)[0]
        self.assertIn('previousState === "flying" && this.model.state === "launching"', frame)
        self.assertIn("if ( reachedLaunchReady ||", " ".join(frame.split()))
        self.assertIn("this.clearAllInput(timestamp, reachedLaunchReady)", frame)

    def test_native_actions_share_keyboard_controller_operations_and_focus_lifecycle(
        self,
    ) -> None:
        listeners = self.game.split("installListeners() {", 1)[1].split("\n    }", 1)[0]
        self.assertIn('this.listen(this.lander_exit, "click", () => this.exit()', listeners)
        self.assertIn(
            'this.listen(this.lander_restart, "click", () => this.restart()',
            listeners,
        )
        keyboard = self.game.split("onKeyDown(event) {", 1)[1].split("onKeyUp(event) {", 1)[0]
        self.assertIn("this.exit()", keyboard)
        self.assertIn("this.restart()", keyboard)
        start = self.game.split("start(holdSpace, timestamp) {", 1)[1].split("exit() {", 1)[0]
        self.assertIn("this.lander_outcome.hidden = false", start)
        self.assertIn("this.lander_exit.disabled = false", start)
        exit_method = self.game.split("exit() {", 1)[1].split("restart() {", 1)[0]
        self.assertIn("this.lander_outcome.hidden = true", exit_method)
        self.assertIn("this.lander_start.focus({ preventScroll: true })", exit_method)
        restart = self.game.split("restart() {", 1)[1].split("activeShellEventPath(event) {", 1)[0]
        self.assertIn("this.lander_restart.hidden = true", restart)
        self.assertIn("this.lander_scene_shell.focus({ preventScroll: true })", restart)
        render = self.game.split("render() {", 1)[1].split("destroy() {", 1)[0]
        self.assertIn('this.model.state === "failed"', render)
        self.assertIn("this.lander_restart.disabled = !failed", render)

    def test_fixed_color_contrast_meets_text_and_graphic_thresholds(self) -> None:
        self.assertGreaterEqual(contrast("#292b30", "#f5f2e8"), 4.5)
        self.assertGreaterEqual(contrast("#4b4e55", "#f5f2e8"), 4.5)
        antenna = contrast("#d94a1e", "#f5f2e8")
        self.assertTrue(math.isclose(antenna, 3.788, abs_tol=0.001))
        self.assertGreaterEqual(antenna, 3.0)

    def test_forbidden_runtime_surfaces_are_absent(self) -> None:
        production = "\n".join((self.template, self.css, self.model, self.game))
        for name, pattern in FORBIDDEN_RUNTIME_PATTERNS.items():
            with self.subTest(name=name):
                self.assertIsNone(re.search(pattern, production))
            for canary in FORBIDDEN_RUNTIME_CANARIES[name]:
                with self.subTest(name=name, canary=canary):
                    self.assertIsNotNone(re.search(pattern, canary))
        for tag in ("audio", "canvas", "iframe"):
            self.assertNotIn(tag, [name for name, _ in self.document.start_tags])


class RocketAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = WEBSITE / "assets" / "agw-rocket.svg"
        cls.source = cls.path.read_text(encoding="utf-8")
        cls.root = ET.fromstring(cls.source)
        cls.by_id = {element.attrib["id"]: element for element in cls.root.iter() if "id" in element.attrib}

    def test_root_is_self_contained_and_named(self) -> None:
        self.assertEqual(self.root.attrib["viewBox"], "0 0 240 520")
        self.assertNotIn("width", self.root.attrib)
        self.assertNotIn("height", self.root.attrib)
        self.assertEqual(self.root.attrib["role"], "img")
        self.assertEqual(
            self.root.attrib["aria-labelledby"],
            "agw-rocket-title agw-rocket-description",
        )
        without_namespace = self.source.replace('xmlns="http://www.w3.org/2000/svg"', "")
        self.assertNotRegex(without_namespace, r"<(?:script|image|animate)\b|https?://")

    def test_stable_global_ids_and_group_hierarchy_are_exact(self) -> None:
        expected = {
            "agw-rocket",
            "agw-rocket-title",
            "agw-rocket-description",
            "agw-plumes",
            "agw-engine-left",
            "agw-engine-right",
            "agw-left-cool-edge",
            "agw-left-warm-middle",
            "agw-left-hot-core",
            "agw-right-cool-edge",
            "agw-right-warm-middle",
            "agw-right-hot-core",
            "agw-mark",
            "agw-letter-w",
            "agw-letter-g",
            "agw-letter-a",
        }
        self.assertEqual(set(self.by_id), expected)

    def test_selected_geometry_and_temperature_layers_are_exact(self) -> None:
        expected_paths = {
            "agw-left-cool-edge": "M60 401c7 9 14 14 22 14s15-5 22-14c-2 42-9 79-22 110-13-31-20-68-22-110Z",
            "agw-left-warm-middle": "M68 408c5 6 9 9 14 9s9-3 14-9c-2 29-6 56-14 78-8-22-12-49-14-78Z",
            "agw-left-hot-core": "M75 412c2 4 5 6 7 6s5-2 7-6c-1 18-3 35-7 49-4-14-6-31-7-49Z",
            "agw-right-cool-edge": "M136 401c7 9 14 14 22 14s15-5 22-14c-2 42-9 79-22 110-13-31-20-68-22-110Z",
            "agw-right-warm-middle": "M144 408c5 6 9 9 14 9s9-3 14-9c-2 29-6 56-14 78-8-22-12-49-14-78Z",
            "agw-right-hot-core": "M151 412c2 4 5 6 7 6s5-2 7-6c-1 18-3 35-7 49-4-14-6-31-7-49Z",
            "agw-letter-w": "M32 274h40l21 68 15-68h24l15 68 21-68h40l-32 112h-35l-21-53-21 53H64L32 274Z",
            "agw-letter-g": (
                "M191 184a34 34 0 0 0-34-34H83a34 34 0 0 0-34 34v78a34 34 0 0 0 34 34h74a34 34 0 0 0 34-34v-22h-73"
            ),
            "agw-letter-a": "M120 10 204 143h-40l-15-27H91l-15 27H36L120 10Zm0 55-18 32h36l-18-32Z",
        }
        for element_id, path in expected_paths.items():
            self.assertEqual(self.by_id[element_id].attrib["d"], path)
        self.assertEqual(self.by_id["agw-letter-w"].attrib["transform"], "translate(0 29)")
        for side in ("left", "right"):
            self.assertEqual(self.by_id[f"agw-{side}-cool-edge"].attrib["fill"], "#d94a1e")
            self.assertEqual(self.by_id[f"agw-{side}-warm-middle"].attrib["fill"], "#ff7a00")
            self.assertEqual(self.by_id[f"agw-{side}-hot-core"].attrib["fill"], "#ffe09a")
        self.assertEqual(self.by_id["agw-mark"].attrib["fill"], "#292b30")


class FaviconAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.favicon_path = WEBSITE / "assets" / "agw-favicon.svg"
        cls.favicon_source = cls.favicon_path.read_text(encoding="utf-8")
        cls.favicon_root = ET.fromstring(cls.favicon_source)
        cls.favicon_by_id = {
            element.attrib["id"]: element for element in cls.favicon_root.iter() if "id" in element.attrib
        }
        rocket_root = ET.fromstring((WEBSITE / "assets" / "agw-rocket.svg").read_text(encoding="utf-8"))
        cls.rocket_by_id = {element.attrib["id"]: element for element in rocket_root.iter() if "id" in element.attrib}

    def test_favicon_is_a_self_contained_flame_free_mark(self) -> None:
        self.assertEqual(self.favicon_root.attrib, {"viewBox": "0 0 240 425"})
        self.assertEqual(
            set(self.favicon_by_id),
            {"agw-mark", "agw-letter-a", "agw-letter-g", "agw-letter-w"},
        )
        without_namespace = self.favicon_source.replace('xmlns="http://www.w3.org/2000/svg"', "")
        self.assertNotRegex(without_namespace, r"<(?:script|style|image|use|animate)\b|https?://")
        self.assertNotRegex(self.favicon_source, r"plume|engine|#d94a1e|#ff7a00|#ffe09a")

    def test_favicon_geometry_matches_the_selected_mark_exactly(self) -> None:
        for element_id in ("agw-mark", "agw-letter-a", "agw-letter-g", "agw-letter-w"):
            with self.subTest(element_id=element_id):
                self.assertEqual(
                    self.favicon_by_id[element_id].attrib,
                    self.rocket_by_id[element_id].attrib,
                )


if __name__ == "__main__":
    unittest.main()
