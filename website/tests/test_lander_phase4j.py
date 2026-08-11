# ruff: noqa: F405

from site_test_support import *  # noqa: F403
from site_game_validation import validate_game_contract


class ArcadeMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fragment = (WEBSITE / "templates/lander-game.html").read_text(encoding="utf-8")
        cls.document = parse(cls.fragment)
        cls.css = (WEBSITE / "static/lander.css").read_text(encoding="utf-8")
        cls.game = (WEBSITE / "static/lander-game.js").read_text(encoding="utf-8")
        cls.model = (WEBSITE / "static/lander-model.js").read_text(encoding="utf-8")

    def element(self, identifier: str) -> tuple[str, dict[str, str | None]]:
        return next(item for item in self.document.start_tags if item[1].get("id") == identifier)

    def test_scene_stage_contains_world_and_chrome_before_the_normal_flow_rail(self) -> None:
        order = [
            "lander-scene-shell",
            "lander-scene-stage",
            "lander-scene",
            "lander-start",
            "lander-fuel",
            "lander-target-direction",
            "lander-outcome",
            "lander-status",
            "lander-restart",
            "lander-controls-rail",
            "lander-controls",
            "lander-exit",
        ]
        positions = [self.fragment.index(f'id="{identifier}"') for identifier in order]
        self.assertEqual(positions, sorted(positions))
        shell = self.css.split("#lander-scene-shell {", 1)[1].split("}", 1)[0]
        stage = self.css.split("#lander-scene-stage {", 1)[1].split("}", 1)[0]
        controls = self.css.rsplit("#lander-controls-rail:not([hidden]) {", 1)[1].split("}", 1)[0]
        self.assertNotIn("aspect-ratio", shell)
        self.assertIn("flex-direction: column", shell)
        self.assertIn("aspect-ratio: 25 / 16", stage)
        self.assertIn("border-block-start: 4px solid #292b30", controls)
        self.assertIn("min-block-size: 44px", controls)
        self.assertNotIn("position: absolute", controls)

    def test_fuel_has_one_hidden_non_live_value_and_one_silent_gauge(self) -> None:
        tags = [tag for tag, _ in self.document.start_tags]
        self.assertNotIn("output", tags)
        self.assertNotIn("meter", tags)
        self.assertNotIn("progress", tags)
        value = self.element("lander-fuel-value")
        label = self.element("lander-fuel-label")
        self.assertEqual(value[0], "span")
        self.assertEqual(value[1], {
            "id": "lander-fuel-value",
            "class": "visually-hidden",
        })
        self.assertEqual(label, ("span", {"id": "lander-fuel-label", "class": "visually-hidden"}))
        self.assertEqual(self.element("lander-fuel-gauge")[1], {
            "id": "lander-fuel-gauge",
            "aria-hidden": "true",
        })
        self.assertEqual(sum(attributes.get("aria-live") == "polite" for _, attributes in self.document.start_tags), 1)
        self.assertEqual(sum(attributes.get("role") == "status" for _, attributes in self.document.start_tags), 1)
        self.assertIn("this.lander_fuel_value.textContent", self.game)
        self.assertNotIn("this.lander_fuel_value.value", self.game)
        self.assertNotIn("data-fuel-band", self.fragment + self.css + self.game)

    def test_action_source_order_identities_and_static_focus_exclusion_are_exact(self) -> None:
        actions = ["lander-restart", "lander-exit"]
        self.assertEqual(
            [self.fragment.index(f'id="{identifier}"') for identifier in actions],
            sorted(self.fragment.index(f'id="{identifier}"') for identifier in actions),
        )
        self.assertEqual(self.element("lander-restart"),
                         ("button", {"id": "lander-restart", "type": "button", "hidden": None,
                                     "disabled": None, "aria-keyshortcuts": "r"}))
        self.assertEqual(self.element("lander-exit"),
                         ("button", {"id": "lander-exit", "type": "button", "disabled": None,
                                     "aria-keyshortcuts": "Escape"}))
        action_rule = self.css.split("#lander-restart,\n#lander-exit {", 1)[1].split("}", 1)[0]
        self.assertIn("min-inline-size: 44px", action_rule)
        self.assertIn("min-block-size: 44px", action_rule)
        self.assertIn("display: inline-grid", action_rule)

    def test_banner_projection_uses_model_state_flags_without_status_copy(self) -> None:
        projection = self.game.split("this.root.dataset.banner =", 1)[1].split(";", 1)[0]
        self.assertNotIn("status", projection)
        for authority in ("launchReady", "failed", 'this.model.state === "generation-error"'):
            self.assertIn(authority, projection)

    def test_mutations_catch_duplicate_authorities_wrong_order_and_missing_static_agent_state(self) -> None:
        restart = re.search(r'<button id="lander-restart"[^>]*>.*?</button>', self.fragment, re.DOTALL)
        self.assertIsNotNone(restart)
        assert restart is not None
        wrong_parent = self.fragment.replace(restart.group(0), "", 1).replace(
            '<button id="lander-exit"', f'{restart.group(0)}\n            <button id="lander-exit"', 1
        )
        mutations = (
            self.fragment.replace('<span id="lander-fuel-value"', '<output id="lander-fuel-value"', 1),
            self.fragment.replace('<span id="lander-fuel-gauge"', '<meter id="lander-fuel-gauge"', 1),
            self.fragment.replace('data-agent="absent" ', "", 1),
            wrong_parent,
            self.fragment.replace('aria-live="polite"', 'aria-live="polite"></p><p aria-live="polite"', 1),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ValueError):
                validate_game_contract(mutation)

    def test_exact_action_rail_shape_rejects_anonymous_wrappers_and_interposed_children(self) -> None:
        outcome = re.search(r'<div id="lander-outcome" hidden>.*?</div>', self.fragment, re.DOTALL)
        rail = re.search(r'<div id="lander-controls-rail" hidden>.*?</div>', self.fragment, re.DOTALL)
        status = re.search(r'<p id="lander-status"[^>]*></p>', self.fragment)
        controls = re.search(r'<p id="lander-controls">.*?</p>', self.fragment, re.DOTALL)
        self.assertTrue(all(match is not None for match in (outcome, rail, status, controls)))
        assert outcome is not None and rail is not None and status is not None and controls is not None
        restart_label = re.search(r'(<button id="lander-restart"[^>]*>\s*<span>)([^<]+)(</span>)', self.fragment)
        self.assertIsNotNone(restart_label)
        assert restart_label is not None
        mutations = (
            self.fragment.replace('<div id="lander-scene-stage">', '<div><div id="lander-scene-stage">', 1)
            .replace('        <div id="lander-controls-rail"', '        </div><div id="lander-controls-rail"', 1),
            self.fragment.replace(rail.group(0), f'<div>{rail.group(0)}</div>', 1),
            self.fragment.replace(outcome.group(0), f'<div>{outcome.group(0)}</div>', 1),
            self.fragment.replace('<div id="lander-outcome" hidden>',
                                  '<span></span><div id="lander-outcome" hidden>', 1),
            self.fragment.replace(status.group(0), f'<div>{status.group(0)}</div>', 1),
            self.fragment.replace(status.group(0), f'{status.group(0)}<span></span>', 1),
            self.fragment.replace(status.group(0), status.group(0).replace('</p>', '<span></span></p>'), 1),
            self.fragment.replace(controls.group(0), f'{controls.group(0)}<span></span>', 1),
            self.fragment.replace(controls.group(0), controls.group(0).replace('</p>', '<span></span></p>'), 1),
            self.fragment.replace('        <div id="lander-controls-rail"',
                                  '        <span></span><div id="lander-controls-rail"', 1),
            self.fragment.replace(restart_label.group(0),
                                  f'{restart_label.group(1)}   {restart_label.group(3)}', 1),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                self.assertNotEqual(mutation, self.fragment)
                with self.assertRaises(ValueError):
                    validate_game_contract(mutation)

    def test_reviewed_root_and_required_tag_identities_reject_stray_structure(self) -> None:
        fuel_label = re.search(r'<span id="lander-fuel-label"[^>]*>.*?</span>', self.fragment, re.DOTALL)
        self.assertIsNotNone(fuel_label)
        assert fuel_label is not None
        wrong_fuel_label_tag = fuel_label.group(0).replace("<span", "<em", 1).replace("</span>", "</em>", 1)
        mutations = (
            f"<div></div>{self.fragment}",
            f"{self.fragment}<span></span>",
            f"stray{self.fragment}",
            f"</div>{self.fragment}",
            self.fragment.replace('<div id="lander-scene-shell"', '<section id="lander-scene-shell"', 1)
            .replace('    </div>\n</section>', '    </section>\n</section>', 1),
            self.fragment.replace('<p id="lander-status"', '<div id="lander-status"', 1)
            .replace('</p>\n                <button id="lander-restart"',
                     '</div>\n                <button id="lander-restart"', 1),
            self.fragment.replace('<button id="lander-restart"', '<a id="lander-restart"', 1)
            .replace('</button>\n            </div>', '</a>\n            </div>', 1),
            self.fragment.replace('<div id="lander-controls-rail"', '<section id="lander-controls-rail"', 1)
            .replace('        </div>\n    </div>', '        </section>\n    </div>', 1),
            self.fragment.replace(fuel_label.group(0), wrong_fuel_label_tag, 1),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                self.assertNotEqual(mutation, self.fragment)
                with self.assertRaises(ValueError):
                    validate_game_contract(mutation)


class ArcadeCssTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.css = (WEBSITE / "static/lander.css").read_text(encoding="utf-8")

    def rule(self, selector: str) -> str:
        return self.css.split(f"{selector} {{", 1)[1].split("}", 1)[0]

    def test_gauge_has_independent_graphite_boundary_level_color_and_height(self) -> None:
        gauge = self.rule("#lander-fuel-gauge")
        fill = self.rule("#lander-fuel-gauge-fill")
        self.assertIn("width: 1rem", gauge)
        self.assertIn("height: 7rem", gauge)
        self.assertIn("border: 3px solid #292b30", gauge)
        self.assertIn("background: #20232a", gauge)
        self.assertIn("inset 0 0 0 3px var(--fuel-level-color)", gauge)
        self.assertIn("background: var(--fuel-level-color)", fill)
        self.assertIn("scaleY(var(--fuel-gauge-level))", fill)
        for color in ("#ff5a36", "#ffb000", "#2ed49b"):
            self.assertIn(color, self.css)
            self.assertGreaterEqual(contrast(color, "#20232a"), 3)

    def test_visually_hidden_fuel_and_direction_use_clipping_not_removal(self) -> None:
        hidden = self.rule(".visually-hidden")
        for declaration in (
            "position: absolute",
            "width: 1px",
            "height: 1px",
            "overflow: hidden",
            "clip: rect(0 0 0 0)",
            "white-space: nowrap",
        ):
            self.assertIn(declaration, hidden)
        self.assertNotRegex(hidden, r"display:\s*none|visibility:\s*hidden|font-size:\s*0")

    def test_pseudo_can_is_the_exact_six_layer_graphite_orange_silhouette(self) -> None:
        block = self.rule('#lander-game[data-refueling="true"] #lander-scene-stage::after')
        for declaration in (
            "inline-size: 20px",
            "block-size: 22px",
            'content: ""',
            "image-rendering: pixelated",
            "pointer-events: none",
            "transform: translate(-50%, -50%)",
            "background-color: transparent",
        ):
            self.assertIn(declaration, block)
        gradients = re.findall(r"linear-gradient\((#[0-9a-f]{6}) 0 0\) ([^,;]+)", block)
        self.assertEqual(gradients, [
            ("#d94a1e", "6px 2px / 6px 2px no-repeat"),
            ("#292b30", "4px 0 / 10px 6px no-repeat"),
            ("#d94a1e", "16px 10px / 2px 4px no-repeat"),
            ("#292b30", "16px 8px / 4px 8px no-repeat"),
            ("#d94a1e", "2px 6px / 12px 14px no-repeat"),
            ("#292b30", "0 4px / 16px 18px no-repeat"),
        ])
        self.assertNotRegex(block, r"\bborder:|\bmask:|\bfilter:|url\(")

    def test_arcade_font_banner_rail_and_touch_scope_have_no_external_surface(self) -> None:
        self.assertIn('ui-monospace, "Cascadia Mono", "Segoe UI Mono", "Liberation Mono"', self.css)
        self.assertNotRegex(self.css, r"@font-face|@import|url\(")
        self.assertIn('data-banner="deployed"', self.css)
        self.assertIn('data-banner="crashed"', self.css)
        self.assertIn("inset 0 -3px 0 #2ed49b", self.css)
        self.assertIn("inset 0 -3px 0 #ff5a36", self.css)
        touch_selectors = re.findall(r"([^{}]+)\{\s*touch-action:\s*none;", self.css)
        self.assertEqual(len(touch_selectors), 1)
        self.assertIn("#lander-scene-stage", touch_selectors[0])
        self.assertNotIn("#lander-scene-shell", touch_selectors[0])

    def test_installed_and_absent_paints_are_exact_and_add_no_css_generated_text(self) -> None:
        absent = self.rule('.lander-site[data-agent="absent"] .noc-entry')
        installed = self.rule('.lander-site[data-agent="installed"] .noc-entry')
        self.assertIn("fill: #3b3f47", absent)
        self.assertIn("stroke: #4b4e55", absent)
        for declaration in (
            "fill: #2ed49b",
            "stroke: #f5f2e8",
            "stroke-width: 1.5",
            "stroke-linecap: round",
            "stroke-linejoin: round",
        ):
            self.assertIn(declaration, installed)
        self.assertEqual(self.css.count('content: ""'), 1)


if __name__ == "__main__":
    unittest.main()
