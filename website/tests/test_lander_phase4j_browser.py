# ruff: noqa: F405

import html
import json
import struct
import threading
import zlib
from decimal import Decimal
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from lander_chromium_phase4k import _probe_source, browser_phase4k_contract, normalize_accessible
from site_test_support import *  # noqa: F403


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def png_pixel(path: Path, x: int, y: int) -> tuple[int, int, int]:
    data = path.read_bytes()
    offset = 8
    chunks: list[bytes] = []
    image_width = image_height = color_type = 0
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        offset += length + 12
        if kind == b"IHDR":
            image_width, image_height, depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", payload)
            if depth != 8 or color_type not in {2, 6} or interlace:
                raise AssertionError("Chromium screenshot used an unsupported PNG encoding")
        elif kind == b"IDAT":
            chunks.append(payload)
    channels = 3 if color_type == 2 else 4
    packed = zlib.decompress(b"".join(chunks))
    stride = image_width * channels
    previous = bytearray(stride)
    rows: list[bytearray] = []
    cursor = 0
    for _ in range(image_height):
        filter_type = packed[cursor]
        cursor += 1
        source = packed[cursor : cursor + stride]
        cursor += stride
        row = bytearray(stride)
        for index, value in enumerate(source):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                estimate = left + above - upper_left
                distances = (abs(estimate - left), abs(estimate - above), abs(estimate - upper_left))
                predictor = (left, above, upper_left)[distances.index(min(distances))]
            else:
                raise AssertionError(f"unsupported PNG filter {filter_type}")
            row[index] = (value + predictor) & 0xFF
        rows.append(row)
        previous = row
    start = x * channels
    return tuple(rows[y][start : start + 3])


def browser_arcade_contract(
    output: Path,
    width: int,
    screenshot: bool = False,
    *,
    paused: bool = False,
    reduced_motion: bool = False,
) -> dict[str, object]:
    chromium = next(
        (candidate for name in ("google-chrome", "chromium", "chromium-browser")
         if (candidate := shutil.which(name))),
        None,
    )
    if chromium is None:
        raise AssertionError("Chromium or Google Chrome is required for Lander arcade browser contracts")
    page = output / "lander/index.html"
    source = page.read_text(encoding="utf-8")
    probe_script = """
const game = document.querySelector("#lander-game");
const stage = document.querySelector("#lander-scene-stage");
const rail = document.querySelector("#lander-controls-rail");
const controls = document.querySelector("#lander-controls");
const fuel = document.querySelector("#lander-fuel");
const fuelLabel = document.querySelector("#lander-fuel-label");
const fuelValue = document.querySelector("#lander-fuel-value");
const gauge = document.querySelector("#lander-fuel-gauge");
const outcome = document.querySelector("#lander-outcome");
const status = document.querySelector("#lander-status");
const restart = document.querySelector("#lander-restart");
const params = new URLSearchParams(location.search);
fuel.hidden = false;
rail.hidden = false;
outcome.hidden = false;
restart.hidden = false;
restart.disabled = false;
status.textContent = "x";
game.dataset.banner = "crashed";
game.dataset.refueling = params.get("refuel") !== "off" ? "true" : "false";
game.dataset.paused = params.get("paused") === "true" ? "true" : "false";
game.dataset.fuelLevel = "empty";
game.style.setProperty("--fuel-level-color", "#ff5a36");
game.style.setProperty("--fuel-gauge-level", "0");
game.style.setProperty("--fuel-transfer-x", "120px");
game.style.setProperty("--fuel-transfer-y", "120px");
const rect = (element) => {
    const value = element.getBoundingClientRect();
    return {top: value.top, right: value.right, bottom: value.bottom, left: value.left,
        width: value.width, height: value.height};
};
const pseudo = getComputedStyle(stage, "::after");
const gaugeStyle = getComputedStyle(gauge);
const actions = [restart, document.querySelector("#lander-exit")].filter((button) => !button.hidden);
const result = {
    stage: rect(stage), rail: rect(rail), controls: rect(controls), gauge: rect(gauge), outcome: rect(outcome),
    status: rect(status), actions: actions.map((button) => ({id: button.id, rect: rect(button)})),
    pseudo: {width: pseudo.width, height: pseudo.height, pointerEvents: pseudo.pointerEvents,
        imageRendering: pseudo.imageRendering, backgroundColor: pseudo.backgroundColor,
        backgroundImage: pseudo.backgroundImage, backgroundSize: pseudo.backgroundSize,
        backgroundPosition: pseudo.backgroundPosition, backgroundRepeat: pseudo.backgroundRepeat,
        transform: pseudo.transform},
    emptyGauge: {animationName: gaugeStyle.animationName, animationDuration: gaugeStyle.animationDuration,
        animationTimingFunction: gaugeStyle.animationTimingFunction,
        animationIterationCount: gaugeStyle.animationIterationCount,
        animationPlayState: gaugeStyle.animationPlayState,
        borderColor: gaugeStyle.borderColor, backgroundColor: gaugeStyle.backgroundColor},
    fontFamily: getComputedStyle(status).fontFamily,
    hiddenFuel: [fuelLabel, fuelValue].map((element) => ({rect: rect(element),
        clip: getComputedStyle(element).clip, display: getComputedStyle(element).display,
        visibility: getComputedStyle(element).visibility})),
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    resources: performance.getEntriesByType("resource").map((entry) => ({name: entry.name,
        initiatorType: entry.initiatorType})),
};
game.dataset.missionState = "flying";
result.touch = {stage: getComputedStyle(stage).touchAction,
    shell: getComputedStyle(document.querySelector("#lander-scene-shell")).touchAction,
    controls: getComputedStyle(controls).touchAction};
document.querySelector("#phase4j-result").textContent = JSON.stringify(result);
"""
    probe = '<pre id="phase4j-result">pending</pre><script src="/phase4j.js"></script>'
    probe_path = output / "phase4j.js"
    probe_path.write_text(probe_script, encoding="utf-8")
    stable_source = re.sub(r'<script type="module" src="[^"]*/lander-game\.js"></script>', "", source)
    page.write_text(stable_source.replace("</body>", f"{probe}</body>", 1), encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(output)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = tempfile.TemporaryDirectory()
    screenshot_paths: list[Path] = []
    try:
        command = [
            chromium,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            f"--user-data-dir={profile.name}",
            f"--window-size={width},1000",
            "--virtual-time-budget=1000",
            "--dump-dom",
        ]
        if reduced_motion:
            command.append("--force-prefers-reduced-motion=reduce")
        query = "?paused=true" if paused else ""
        command.append(f"http://127.0.0.1:{server.server_address[1]}/lander/{query}")
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "HOME": profile.name},
        )
        if screenshot:
            for state in ("on", "off"):
                screenshot_path = Path(profile.name) / f"can-{state}.png"
                subprocess.run(
                    (
                        chromium,
                        "--headless",
                        "--disable-gpu",
                        "--no-sandbox",
                        "--hide-scrollbars",
                        f"--user-data-dir={Path(profile.name) / f'profile-{state}'}",
                        f"--window-size={width},1000",
                        "--virtual-time-budget=1000",
                        f"--screenshot={screenshot_path}",
                        f"http://127.0.0.1:{server.server_address[1]}/lander/?refuel={state}",
                    ),
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    env={**os.environ, "HOME": profile.name},
                )
                screenshot_paths.append(screenshot_path)
            match = re.search(r'<pre id="phase4j-result">([^<]+)</pre>', completed.stdout)
            if match is None:
                raise AssertionError("Chromium omitted Phase 4J screenshot geometry")
            geometry = json.loads(html.unescape(match.group(1)))
            center_x = round(geometry["stage"]["left"] + 120)
            center_y = round(geometry["stage"]["top"] + 120)
            probes = ((5, 1), (7, 3), (18, 9), (16, 11), (1, 5), (3, 7), (0, 0), (19, 21))
            geometry["canPixels"] = [png_pixel(screenshot_paths[0], center_x - 10 + x, center_y - 11 + y)
                                     for x, y in probes]
            geometry["offPixels"] = [png_pixel(screenshot_paths[1], center_x - 10 + x, center_y - 11 + y)
                                     for x, y in probes]
    finally:
        page.write_text(source, encoding="utf-8")
        probe_path.unlink(missing_ok=True)
        profile.cleanup()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    match = re.search(r'<pre id="phase4j-result">([^<]+)</pre>', completed.stdout)
    if match is None or match.group(1) == "pending":
        raise AssertionError(f"Chromium did not return Phase 4J geometry: {completed.stderr[-1000:]}")
    result = json.loads(html.unescape(match.group(1)))
    if screenshot:
        result.update({key: geometry[key] for key in ("canPixels", "offPixels")})
    return result


def browser_description_contract(output: Path) -> dict[str, object]:
    chromium = next(
        (candidate for name in ("google-chrome", "chromium", "chromium-browser")
         if (candidate := shutil.which(name))),
        None,
    )
    if chromium is None:
        raise AssertionError("Chromium or Google Chrome is required for Lander description contracts")
    page = output / "lander/index.html"
    source = page.read_text(encoding="utf-8")
    probe_script = """
import { landerGameController as controller } from "/static/lander-game.js";
const shell = document.querySelector("#lander-scene-shell");
const direction = document.querySelector("#lander-target-direction");
const resultElement = document.querySelector("#phase4j-description-result");
controller.start(false, performance.now());
const targetId = controller.model.targetSiteId;
const cameraLeft = Math.max(0, controller.model.pose.x - 35);
const withTargetLeft = (platformLeft) => {
    controller.model = {...controller.model, retainedSites: controller.model.retainedSites.map((site) =>
        site.id === targetId ? {...site, platformLeft} : site)};
    controller.render();
};
const snapshot = () => ({
    ids: shell.getAttribute("aria-describedby").split(" "),
    resolved: shell.ariaDescribedByElements.map((element) => element.id),
    directionHidden: direction.hidden,
});
withTargetLeft(cameraLeft + 100);
const boundary = snapshot();
withTargetLeft(cameraLeft + 100 + 1e-9);
const offscreen = snapshot();
resultElement.textContent = JSON.stringify({boundary, offscreen});
controller.destroy();
"""
    probe = ('<pre id="phase4j-description-result">pending</pre>'
             '<script type="module" src="/phase4j-description.js"></script>')
    probe_path = output / "phase4j-description.js"
    probe_path.write_text(probe_script, encoding="utf-8")
    page.write_text(source.replace("</body>", f"{probe}</body>", 1), encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(output)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = tempfile.TemporaryDirectory()
    try:
        completed = subprocess.run(
            (
                chromium,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--hide-scrollbars",
                f"--user-data-dir={profile.name}",
                "--window-size=960,1000",
                "--virtual-time-budget=1000",
                "--dump-dom",
                f"http://127.0.0.1:{server.server_address[1]}/lander/",
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "HOME": profile.name},
        )
    finally:
        page.write_text(source, encoding="utf-8")
        probe_path.unlink(missing_ok=True)
        profile.cleanup()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    match = re.search(r'<pre id="phase4j-description-result">([^<]+)</pre>', completed.stdout)
    if match is None or match.group(1) == "pending":
        raise AssertionError(f"Chromium did not return Phase 4J descriptions: {completed.stderr[-1000:]}")
    return json.loads(html.unescape(match.group(1)))


class ArcadeBrowserTests(RepositoryFixture):
    def assert_released_input(self, witness: dict[str, object], *, cleared_queue: bool) -> None:
        zero = {"left": 0, "right": 0, "vectorAngle": 0}
        self.assertEqual(witness["held"], [])
        self.assertEqual(witness["commanded"], zero)
        self.assertEqual(witness["input"], zero)
        self.assertIsNone(witness["pointer"])
        self.assertEqual(witness["pointerInput"], zero)
        self.assertEqual(witness["pulse"], {"active": False, "token": None, "deadline": None})
        self.assertIsNone(witness["releasedCapture"])
        queue = witness["queue"]
        self.assertTrue(queue)
        if cleared_queue:
            self.assertTrue(all(edge["left"] == 0 and edge["right"] == 0 for edge in queue))
            self.assertEqual(len(queue), 1)
            self.assertEqual(set(queue[0]) - {"timestamp", "sequence"}, {"left", "right"})
        else:
            self.assertEqual(queue[-1]["left"], 0)
            self.assertEqual(queue[-1]["right"], 0)

    def assert_phase4k_release_contract(self, result: dict[str, object]) -> None:
        native = result["nativeRelease"]
        self.assertEqual(native["before"]["focus"], "lander-scene-shell")
        self.assertEqual(native["pressed"]["held"], ["Space"])
        self.assertTrue(any(edge["left"] > 0 and edge["right"] > 0 for edge in native["pressed"]["queue"]))
        self.assert_released_input(native["released"], cleared_queue=False)
        self.assertEqual(native["events"], [
            {"type": "keydown", "key": " ", "defaultPrevented": True},
            {"type": "keyup", "key": " ", "defaultPrevented": True},
        ])

        focusout = result["focusoutRelease"]
        self.assertEqual(focusout["before"]["focus"], "lander-scene-shell")
        self.assertEqual(focusout["pressed"]["held"], ["Space"])
        self.assertTrue(any(edge["left"] > 0 and edge["right"] > 0
                            for edge in focusout["pressed"]["queue"]))
        self.assertEqual(focusout["focusout"]["focus"], "phase4k-header-target")
        self.assert_released_input(focusout["focusout"], cleared_queue=True)
        for key in ("state", "launchStarted", "pose", "fuel", "clicks"):
            self.assertEqual(focusout["focusout"][key], focusout["before"][key])
        self.assertEqual(focusout["afterOutsideKeyup"], focusout["focusout"])
        self.assertEqual(focusout["outsideEvents"], [
            {"type": "keyup", "key": " ", "defaultPrevented": False},
        ])

    def parse_fixed_tenth(self, value: str) -> Decimal:
        self.assertIsNotNone(re.fullmatch(r"\d+\.\d", value))
        parsed = Decimal(value)
        self.assertTrue(parsed.is_finite())
        self.assertEqual(parsed.as_tuple().exponent, -1)
        return parsed

    def assert_phase4k_accessibility_contract(self, accessibility: dict[str, object]) -> None:
        dom = accessibility["dom"]
        refresh = accessibility["refresh"]
        self.assertEqual(dom["ids"].index("lander-fuel-value"), dom["ids"].index("lander-fuel-label") + 1)
        stable_label = dom["label"]
        stable_status = dom["status"]
        self.assertEqual(refresh["before"]["label"].encode(), stable_label.encode())
        self.assertEqual(refresh["after"]["label"].encode(), stable_label.encode())
        self.assertEqual(refresh["before"]["status"].encode(), stable_status.encode())
        self.assertEqual(refresh["after"]["status"].encode(), stable_status.encode())
        self.assertEqual(refresh["before"]["value"], dom["value"])
        before_value = self.parse_fixed_tenth(refresh["before"]["value"])
        after_value = self.parse_fixed_tenth(refresh["after"]["value"])
        self.assertEqual(before_value - after_value, Decimal("0.1"))
        before_segment = normalize_accessible(f'{stable_label} {refresh["before"]["value"]}')
        after_segment = normalize_accessible(f'{stable_label} {refresh["after"]["value"]}')
        before_description = normalize_accessible(accessibility["beforeDescription"])
        after_description = normalize_accessible(accessibility["afterDescription"])
        self.assertEqual(before_description.count(before_segment), 1)
        self.assertEqual(after_description.count(after_segment), 1)
        self.assertNotIn(before_segment, after_description)
        self.assertEqual(after_description, before_description.replace(before_segment, after_segment, 1))
        for action in ("restart", "exit"):
            copy = dom["actionCopy"][action]
            self.assertIn(copy["label"], accessibility["buttonNames"])
            self.assertNotIn(f'{copy["label"]} {copy["hint"]}', accessibility["buttonNames"])

    def test_phase4k_native_input_focus_departure_and_accessibility_contracts(self) -> None:
        result = browser_phase4k_contract(self.build())
        self.assertEqual(result["activeTabOrder"], ["lander-exit"])
        self.assertEqual(result["failedTabOrder"], ["lander-restart", "lander-exit"])
        for activation in result["activations"]:
            with self.subTest(action=activation["action"], key=activation["key"]):
                self.assertEqual(activation["clicks"], 1)
                self.assertEqual(activation["held"], [])
                self.assertFalse(activation["thrustEdges"])
                self.assertEqual(activation["commanded"], {"left": 0, "right": 0, "vectorAngle": 0})
                self.assertNotEqual(activation["state"],
                                    "flying" if activation["action"] == "exit" else "failed")
        for witness in result["passiveActions"]:
            with self.subTest(action=witness["action"], key=witness["key"]):
                self.assertEqual(witness["after"], witness["before"])
        self.assert_phase4k_release_contract(result)

        outside = result["outside"]
        expected_matrix = {
            (state, target, key)
            for state in ("flying", "launching", "failed")
            for target in ("header", "breadcrumb")
            for key in ("Escape", "KeyR", "Space", "ArrowUp", "ArrowDown", "ArrowLeft",
                        "ArrowRight", "KeyH", "KeyL")
        }
        self.assertEqual({(item["state"], item["target"], item["key"]) for item in outside},
                         expected_matrix)
        for witness in outside:
            with self.subTest(state=witness["state"], target=witness["target"], key=witness["key"]):
                self.assertEqual(witness["before"]["focus"], f'phase4k-{witness["target"]}-target')
                self.assertEqual(witness["after"], witness["before"])
                self.assertEqual(len(witness["events"]), 2)
                self.assertTrue(all(not event["defaultPrevented"] for event in witness["events"]))

        self.assertEqual([witness["authority"] for witness in result["departures"]],
                         ["keyboard-hold", "keyboard-tap", "vi-hold", "vi-tap",
                          "mouse-hold", "mouse-tap",
                          "touch-hold", "touch-tap"])
        for witness in result["departures"]:
            self.assertTrue(witness["launchStarted"], witness["authority"])
            self.assertTrue(witness["fuelSpent"], witness["authority"])

        self.assert_phase4k_accessibility_contract(result["accessibility"])

    def test_phase4k_browser_departures_require_production_input_listeners(self) -> None:
        output = self.build()
        game_path = output / "static/lander-game.js"
        source = game_path.read_text(encoding="utf-8")
        listeners = (
            'this.listen(document, "keydown", (event) => this.onKeyDown(event));',
            'this.listen(document, "keyup", (event) => this.onKeyUp(event));',
            'this.listen(this.lander_scene_shell, "focusout", () => this.clearAllInput(performance.now()));',
            'this.listen(this.lander_scene_stage, type, (event) => this.onPointer(event));',
        )
        for listener in listeners:
            with self.subTest(listener=listener):
                self.assertIn(listener, source)
                try:
                    game_path.write_text(source.replace(listener, "", 1), encoding="utf-8")
                    with self.assertRaises(AssertionError):
                        result = browser_phase4k_contract(output)
                        self.assert_phase4k_release_contract(result)
                finally:
                    game_path.write_text(source, encoding="utf-8")

    def test_phase4k_accessibility_rejects_nonnumeric_fuel_mutations(self) -> None:
        output = self.build()
        probe = _probe_source()
        setup_anchor = (
            '        const status = document.querySelector("#lander-status");\n'
            '        return {ids: shell.getAttribute("aria-describedby").split(" "),'
        )
        refresh_anchor = (
            "        controller.render();\n"
            "        return {before, after: {label: label.textContent.trim(), value: value.textContent.trim(),"
        )
        mutations = (
            probe.replace(setup_anchor, setup_anchor.replace("        return", "        value.textContent = label.id;\n        return"), 1),
            probe.replace(refresh_anchor,
                          refresh_anchor.replace("        return", "        value.textContent = status.id;\n        return"), 1),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation != probe):
                self.assertNotEqual(mutation, probe)
                with self.assertRaises(AssertionError):
                    result = browser_phase4k_contract(output, probe_source_factory=lambda: mutation)
                    self.assert_phase4k_accessibility_contract(result["accessibility"])

    def test_active_description_resolves_only_visible_conditional_relationships(self) -> None:
        result = browser_description_contract(self.build())
        permanent = ["lander-scene-description", "lander-controls", "lander-fuel-label",
                     "lander-fuel-value", "lander-status"]
        self.assertEqual(result["boundary"], {
            "ids": permanent,
            "resolved": permanent,
            "directionHidden": True,
        })
        offscreen = [*permanent[:-1], "lander-target-direction", permanent[-1]]
        self.assertEqual(result["offscreen"], {
            "ids": offscreen,
            "resolved": offscreen,
            "directionHidden": False,
        })

    def test_narrow_and_full_width_chrome_stays_inside_the_stage_above_the_rail(self) -> None:
        output = self.build()
        for width in (320, 960):
            with self.subTest(width=width):
                result = browser_arcade_contract(output, width)
                stage = result["stage"]
                rail = result["rail"]
                gauge = result["gauge"]
                outcome = result["outcome"]
                self.assertLessEqual(stage["bottom"], rail["top"])
                self.assertGreaterEqual(gauge["left"], stage["left"])
                self.assertLessEqual(gauge["right"], stage["right"])
                self.assertGreaterEqual(outcome["left"], stage["left"])
                self.assertLessEqual(outcome["right"], stage["right"])
                self.assertTrue(gauge["right"] <= outcome["left"] or outcome["right"] <= gauge["left"])
                self.assertEqual([action["id"] for action in result["actions"]],
                                 ["lander-restart", "lander-exit"])
                exit_rect = result["actions"][1]["rect"]
                if width <= 512:
                    self.assertLessEqual(result["controls"]["bottom"], exit_rect["top"])
                else:
                    self.assertLessEqual(result["controls"]["right"], exit_rect["left"])
                for action in result["actions"]:
                    self.assertGreaterEqual(action["rect"]["width"], 44)
                    self.assertGreaterEqual(action["rect"]["height"], 44)
                self.assertLessEqual(result["status"]["bottom"], result["actions"][0]["rect"]["top"])
                self.assertEqual(result["overflow"], 0)

    def test_computed_pseudo_can_font_touch_scope_and_requests_match_the_contract(self) -> None:
        result = browser_arcade_contract(self.build(), 960, screenshot=True)
        self.assertEqual(result["emptyGauge"], {
            "animationName": "agw-fuel-empty-blink",
            "animationDuration": "0.7s",
            "animationTimingFunction": "steps(1)",
            "animationIterationCount": "infinite",
            "animationPlayState": "running",
            "borderColor": "rgb(255, 90, 54)",
            "backgroundColor": "rgb(255, 90, 54)",
        })
        pseudo = result["pseudo"]
        self.assertEqual(pseudo["width"], "20px")
        self.assertEqual(pseudo["height"], "22px")
        self.assertEqual(pseudo["pointerEvents"], "none")
        self.assertEqual(pseudo["imageRendering"], "pixelated")
        self.assertEqual(pseudo["backgroundColor"], "rgba(0, 0, 0, 0)")
        self.assertEqual(len(pseudo["backgroundImage"].split("linear-gradient")) - 1, 6)
        self.assertEqual(pseudo["backgroundSize"],
                         "6px 2px, 10px 6px, 2px 4px, 4px 8px, 12px 14px, 16px 18px")
        self.assertEqual(pseudo["backgroundPosition"],
                         "6px 2px, 4px 0px, 16px 10px, 16px 8px, 2px 6px, 0px 4px")
        self.assertEqual(pseudo["backgroundRepeat"],
                         "no-repeat, no-repeat, no-repeat, no-repeat, no-repeat, no-repeat")
        graphite = (41, 43, 48)
        orange = (217, 74, 30)
        self.assertEqual(result["canPixels"][:6], [graphite, orange, graphite, orange, graphite, orange])
        self.assertEqual(result["canPixels"][6:], result["offPixels"][6:])
        self.assertIn("Cascadia Mono", result["fontFamily"])
        self.assertEqual(result["touch"], {"stage": "none", "shell": "auto", "controls": "auto"})
        for hidden in result["hiddenFuel"]:
            self.assertEqual(hidden["rect"]["width"], 1)
            self.assertEqual(hidden["rect"]["height"], 1)
            self.assertEqual(hidden["clip"], "rect(0px, 0px, 0px, 0px)")
            self.assertNotEqual(hidden["display"], "none")
            self.assertEqual(hidden["visibility"], "visible")
        self.assertTrue(all(resource["initiatorType"] != "font" for resource in result["resources"]))
        self.assertTrue(all(resource["name"].startswith("http://127.0.0.1:") for resource in result["resources"]))

    def test_empty_gauge_pauses_and_reduced_motion_keeps_a_static_red_warning(self) -> None:
        output = self.build()
        paused = browser_arcade_contract(output, 960, paused=True)["emptyGauge"]
        self.assertEqual(paused["animationName"], "agw-fuel-empty-blink")
        self.assertEqual(paused["animationPlayState"], "paused")
        reduced = browser_arcade_contract(output, 960, reduced_motion=True)["emptyGauge"]
        self.assertEqual(reduced["animationName"], "none")
        self.assertEqual(reduced["borderColor"], "rgb(255, 90, 54)")
        self.assertEqual(reduced["backgroundColor"], "rgb(255, 90, 54)")


if __name__ == "__main__":
    unittest.main()
