# ruff: noqa: F405

import html
import json
import struct
import threading
import zlib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

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


def browser_arcade_contract(output: Path, width: int, screenshot: bool = False) -> dict[str, object]:
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
fuel.hidden = false;
rail.hidden = false;
outcome.hidden = false;
restart.hidden = false;
restart.disabled = false;
status.textContent = "x";
game.dataset.banner = "crashed";
game.dataset.refueling = new URLSearchParams(location.search).get("refuel") !== "off" ? "true" : "false";
game.dataset.fuelLevel = "danger";
game.style.setProperty("--fuel-level-color", "#ff5a36");
game.style.setProperty("--fuel-gauge-level", "0.25");
game.style.setProperty("--fuel-transfer-x", "120px");
game.style.setProperty("--fuel-transfer-y", "120px");
const rect = (element) => {
    const value = element.getBoundingClientRect();
    return {top: value.top, right: value.right, bottom: value.bottom, left: value.left,
        width: value.width, height: value.height};
};
const pseudo = getComputedStyle(stage, "::after");
const actions = [restart, document.querySelector("#lander-exit")].filter((button) => !button.hidden);
const result = {
    stage: rect(stage), rail: rect(rail), controls: rect(controls), gauge: rect(gauge), outcome: rect(outcome),
    status: rect(status), actions: actions.map((button) => ({id: button.id, rect: rect(button)})),
    pseudo: {width: pseudo.width, height: pseudo.height, pointerEvents: pseudo.pointerEvents,
        imageRendering: pseudo.imageRendering, backgroundColor: pseudo.backgroundColor,
        backgroundImage: pseudo.backgroundImage, backgroundSize: pseudo.backgroundSize,
        backgroundPosition: pseudo.backgroundPosition, backgroundRepeat: pseudo.backgroundRepeat,
        transform: pseudo.transform},
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
        completed = subprocess.run(
            (
                chromium,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--hide-scrollbars",
                f"--user-data-dir={profile.name}",
                f"--window-size={width},1000",
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
                self.assertLessEqual(result["controls"]["right"], exit_rect["left"])
                for action in result["actions"]:
                    self.assertGreaterEqual(action["rect"]["width"], 44)
                    self.assertGreaterEqual(action["rect"]["height"], 44)
                self.assertLessEqual(result["status"]["bottom"], result["actions"][0]["rect"]["top"])
                self.assertEqual(result["overflow"], 0)

    def test_computed_pseudo_can_font_touch_scope_and_requests_match_the_contract(self) -> None:
        result = browser_arcade_contract(self.build(), 960, screenshot=True)
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


if __name__ == "__main__":
    unittest.main()
