"""Real-Chromium witnesses for Phase 4M exploration, sky, and fuel projection."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from lander_chromium_phase4k import DevToolsConnection, _devtools_target, _QuietHandler
from site_test_support import RepositoryFixture

PROBE = r"""
import { landerGameController as controller } from "/static/lander-game.js";
import { createRun, fuelGaugeLevel, stepFlight } from "/static/lander-model.js";

if (controller.frameId !== null) cancelAnimationFrame(controller.frameId);
controller.frameId = null;
controller.paused = true;
const root = document.querySelector("#lander-game");
const stars = document.querySelector("#scene-stars");
const landmarks = document.querySelector("#scene-landmarks");
const sky = document.querySelector("#lander-sky-world");
const zero = {left: 0, right: 0};
const snapshot = () => ({
    state: controller.model.state,
    camera: root.style.getPropertyValue("--camera-x"),
    skyCamera: root.style.getPropertyValue("--sky-camera-x"),
    direction: root.dataset.targetDirection,
    skyChildren: sky.children.length,
    stars: stars.getAttribute("d"),
    landmarks: landmarks.getAttribute("d"),
    starCount: (stars.getAttribute("d").match(/M/g) || []).length,
});
const project = (model, x) => {
    controller.model = {...model, pose: {...model.pose, x, y: 30, vx: x < 0 ? -1 : 1,
        vy: 0, angle: 0, angularVelocity: 0}};
    controller.render();
    return snapshot();
};
let run = createRun({seed: 1});
const opening = {fuel: run.fuel, reference: run.fuelGaugeReference, level: fuelGaugeLevel(run)};
const crossings = [-5.01, 101.01].map((x) => {
    const model = stepFlight({...run, pose: {x, y: 30, vx: x < 0 ? -1 : 1,
        vy: 0, angle: 0, angularVelocity: 0}}, zero);
    return {x, state: model.state, cause: model.failureCause};
});
const right = project(run, -80);
const left = project(run, 160);
const returned = project(run, -80);
const target = run.retainedSites.find((site) => site.id === run.targetSiteId);
let powered = stepFlight({...run, reducedMotion: true,
    pose: {x: target.center, y: target.platformTop + .001,
        vx: 0, vy: -1, angle: 0, angularVelocity: 0}}, zero);
controller.model = powered;
controller.render();
const awarded = {state: powered.state, fuel: powered.fuel, reference: powered.fuelGaugeReference,
    level: fuelGaugeLevel(powered), projected: root.style.getPropertyValue("--fuel-gauge-level")};
window.phase4m = {opening, crossings, right, left, returned, awarded};
document.documentElement.dataset.phase4mReady = "true";
"""


def browser_phase4m_contract(output: Path) -> dict[str, object]:
    chromium = next(
        (candidate for name in ("google-chrome", "chromium", "chromium-browser") if (candidate := shutil.which(name))),
        None,
    )
    if chromium is None:
        raise AssertionError("Chromium or Google Chrome is required for Phase 4M browser contracts")
    page = output / "lander/index.html"
    probe_path = output / "phase4m-browser.js"
    source = page.read_text(encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(output)))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="phase4m-browser-server")
    profile = tempfile.TemporaryDirectory()
    process: subprocess.Popen[bytes] | None = None
    connection: DevToolsConnection | None = None
    try:
        probe_path.write_text(PROBE, encoding="utf-8")
        page.write_text(
            source.replace("</body>", '<script type="module" src="/phase4m-browser.js"></script></body>', 1),
            encoding="utf-8",
        )
        thread.start()
        process = subprocess.Popen(
            (
                chromium,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                "--remote-allow-origins=*",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile.name}",
                "about:blank",
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "HOME": profile.name},
        )
        connection = DevToolsConnection(_devtools_target(Path(profile.name), process))
        for domain in ("Runtime", "Page"):
            connection.call(f"{domain}.enable")
        connection.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_address[1]}/lander/"})
        for _ in range(240):
            if connection.evaluate("document.documentElement.dataset.phase4mReady === 'true'"):
                return connection.evaluate("phase4m")
            time.sleep(0.025)
        raise AssertionError("Phase 4M browser probe did not initialize")
    finally:
        if connection is not None:
            connection.close()
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        if thread.is_alive():
            thread.join(timeout=5)
        page.write_text(source, encoding="utf-8")
        probe_path.unlink(missing_ok=True)
        profile.cleanup()


class Phase4MBrowserTests(RepositoryFixture):
    def test_real_chromium_projects_free_exploration_parallax_and_honest_fuel(self) -> None:
        result = browser_phase4m_contract(self.build())
        self.assertEqual(result["opening"], {"fuel": 15, "reference": 30, "level": 0.5})
        self.assertTrue(all(item["state"] == "flying" for item in result["crossings"]))
        self.assertTrue(all(item["cause"] is None for item in result["crossings"]))
        self.assertEqual(
            [result[key]["direction"] for key in ("right", "left", "returned")], ["right", "left", "right"]
        )
        for key in ("right", "left", "returned"):
            projection = result[key]
            self.assertEqual(projection["skyChildren"], 2)
            self.assertEqual(projection["starCount"], 20)
            self.assertTrue(projection["landmarks"])
            camera = float(projection["camera"].removesuffix("px"))
            sky_camera = float(projection["skyCamera"].removesuffix("px"))
            self.assertAlmostEqual(sky_camera, camera * 0.24)
        self.assertEqual(result["returned"]["stars"], result["right"]["stars"])
        self.assertEqual(result["returned"]["landmarks"], result["right"]["landmarks"])
        awarded = result["awarded"]
        self.assertEqual(awarded["state"], "launching")
        self.assertGreater(awarded["fuel"], 30)
        self.assertEqual(awarded["fuel"], awarded["reference"])
        self.assertEqual(awarded["level"], 1)
        self.assertEqual(awarded["projected"], "1")
