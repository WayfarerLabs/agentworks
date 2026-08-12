"""Real-Chromium witnesses for the Phase 4P terrain and camera projection."""

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
import { createRun, stepFlight, updateRetention } from "/static/lander-model.js";
import { STATIC_WORLD_SEED, cameraForPose, siteScaffoldMembers,
    siteStructure, terrainHeightAt, terrainNormalizedHeightAt, terrainSurfacePath, terrainVerticesForWindow,
    worldSceneX, worldSceneY, worldViewportX, worldViewportY } from "/static/lander-world.js";

try {
if (controller.frameId !== null) cancelAnimationFrame(controller.frameId);
controller.frameId = null;
controller.paused = true;
const root = document.querySelector("#lander-game");
const world = document.querySelector("#lander-world");
const stage = document.querySelector("#lander-scene-stage");
const rail = document.querySelector("#lander-controls-rail");
const scene = document.querySelector("#lander-scene");
const rect = (element) => {
    const value = element.getBoundingClientRect();
    return {top: value.top, right: value.right, bottom: value.bottom, left: value.left,
        width: value.width, height: value.height};
};
const paintRect = (element) => {
    const bounds = rect(element);
    const halfStroke = Number.parseFloat(getComputedStyle(element).strokeWidth) / 2 || 0;
    return {top: bounds.top - halfStroke, right: bounds.right + halfStroke,
        bottom: bounds.bottom + halfStroke, left: bounds.left - halfStroke,
        width: bounds.width + 2 * halfStroke, height: bounds.height + 2 * halfStroke};
};
const overlaps = (left, right) => left.left < right.right && left.right > right.left &&
    left.top < right.bottom && left.bottom > right.top;
const inside = (inner, outer) => inner.left >= outer.left - .5 && inner.right <= outer.right + .5 &&
    inner.top >= outer.top - .5 && inner.bottom <= outer.bottom + .5;
const pairwiseOverlaps = (entries) => entries.flatMap(([leftName, left], index) =>
    entries.slice(index + 1).map(([rightName, right]) => ({
        pair: [leftName, rightName], overlaps: overlaps(left, right),
    })));
let model = updateRetention(createRun({seed: STATIC_WORLD_SEED}));
const site = model.retainedSites[0];
const staticTerrain = document.querySelector(".terrain-surface").getAttribute("d");
const staticTerrainParity = staticTerrain === terrainSurfacePath(
    terrainVerticesForWindow(STATIC_WORLD_SEED, [site], 0, 100));
controller.model = model;
controller.render();
const runtimeTerrain = document.querySelector(".terrain-surface").getAttribute("d");
const group = document.querySelector(`[data-site-id="${site.id}"]`);
const scaffoldMembers = siteScaffoldMembers(site);
const supportColumns = siteStructure(site).supportColumns;
const terrainNode = document.querySelector(".terrain-surface");
const scaffoldNode = group.querySelector(".site-scaffold");
const sceneBounds = rect(scene);
const visibleFeet = supportColumns
    .flatMap((column) => [[column.left, column.leftFoot], [column.right, column.rightFoot]])
    .map(([x, y]) => {
        const terrainPoint = new DOMPoint(worldSceneX(x), worldSceneY(y)).matrixTransform(terrainNode.getScreenCTM());
        const scaffoldPoint = new DOMPoint(worldSceneX(x), worldSceneY(y)).matrixTransform(scaffoldNode.getScreenCTM());
        return {x, y, terrainX: terrainPoint.x, terrainY: terrainPoint.y,
            scaffoldX: scaffoldPoint.x, scaffoldY: scaffoldPoint.y,
            terrainVertex: model.terrainVertices.some(([vertexX, vertexY]) => vertexX === x && vertexY === y),
            inside: terrainPoint.x >= sceneBounds.left && terrainPoint.x <= sceneBounds.right &&
                terrainPoint.y >= sceneBounds.top && terrainPoint.y <= sceneBounds.bottom};
    });
const opening = {
    cameraX: root.style.getPropertyValue("--camera-x"),
    cameraY: root.style.getPropertyValue("--camera-y"),
    deckLevel: site.deckLevel,
    staticTerrainParity,
    runtimeTerrainParity: runtimeTerrain === terrainSurfacePath(model.terrainVertices),
    scaffoldMembers: (group.querySelector(".site-scaffold").getAttribute("d").match(/M/g) || []).length,
    expectedMembers: scaffoldMembers.length,
    visibleFeet,
    feet: [site.platformLeft, site.platformLeft + 1, site.platformLeft + 8.8,
        site.platformLeft + 9.8, site.platformLeft + 17.6, site.platformLeft + 18.6]
        .map((x) => terrainHeightAt(site.seed, x)),
};
let maximum = updateRetention(createRun({seed: STATIC_WORLD_SEED, reducedMotion: true}));
maximum = updateRetention({...maximum, pose: {...maximum.pose, x: 30, y: 56,
    vx: 0, vy: 0, angle: 0, angularVelocity: 0}});
controller.model = maximum;
controller.render();
const maximumCamera = cameraForPose(maximum.pose);
const maximumSite = maximum.retainedSites.find((candidate) => candidate.id === maximum.targetSiteId);
const maximumGroup = document.querySelector(`[data-site-id="${maximumSite.id}"]`);
const maximumTerrain = document.querySelector(".terrain-surface");
const maximumScaffold = maximumGroup.querySelector(".site-scaffold");
const maximumStageBounds = rect(stage);
const maximumSceneBounds = rect(scene);
const maximumMembers = siteScaffoldMembers(maximumSite);
const maximumSupportFeet = siteStructure(maximumSite).supportColumns
    .flatMap((column) => [[column.left, column.leftFoot], [column.right, column.rightFoot]])
    .map(([x, y]) => {
        const terrainPoint = new DOMPoint(worldSceneX(x), worldSceneY(y))
            .matrixTransform(maximumTerrain.getScreenCTM());
        const scaffoldPoint = new DOMPoint(worldSceneX(x), worldSceneY(y))
            .matrixTransform(maximumScaffold.getScreenCTM());
        return {x, y, terrainX: terrainPoint.x, terrainY: terrainPoint.y,
            scaffoldX: scaffoldPoint.x, scaffoldY: scaffoldPoint.y,
            terrainVertex: maximum.terrainVertices.some(([vertexX, vertexY]) => vertexX === x && vertexY === y),
            scaffoldEndpoint: maximumMembers.some(({end}) => end[0] === x && end[1] === y),
            terrainClipped: terrainPoint.y > maximumStageBounds.bottom,
            scaffoldClipped: scaffoldPoint.y > maximumStageBounds.bottom};
    });
const maximumTarget = {
    direction: root.dataset.targetDirection,
    cue: rect(document.querySelector("#next-site-cue")),
    landingFace: paintRect(maximumGroup.querySelector(".landing-platform")),
    noc: paintRect(maximumGroup.querySelector(".noc-building:not(.noc-entry)")),
    mast: paintRect(maximumGroup.querySelector(".antenna-mast")),
};
const maximumWindow = {
    camera: maximumCamera,
    terrainParity: maximumTerrain.getAttribute("d") === terrainSurfacePath(maximum.terrainVertices),
    supportFeet: maximumSupportFeet,
    target: {...maximumTarget,
        landingFaceInside: inside(maximumTarget.landingFace, maximumSceneBounds),
        nocInside: inside(maximumTarget.noc, maximumSceneBounds),
        mastInside: inside(maximumTarget.mast, maximumSceneBounds)},
};
model = {...model, pose: {...model.pose, x: 160, y: 55, vx: 0, vy: 0}};
controller.model = model;
controller.render();
const expectedCamera = cameraForPose(model.pose);
const explored = {
    cameraX: root.style.getPropertyValue("--camera-x"),
    cameraY: root.style.getPropertyValue("--camera-y"),
    expectedCamera,
    descendants: world.querySelectorAll("*").length,
    targetDirection: root.dataset.targetDirection,
};
const reliefVectors = [
    {name: "basin", seed: 11, anchorX: -640, normalized: 0.10337466620840133,
        pose: {x: -683.3, y: 11.6}, camera: {left: -690, down: 0}, viewportY: 573.8402136266232,
        targetDirection: "right"},
    {name: "ridge", seed: 41, anchorX: 1920, normalized: 0.5986480843508616,
        pose: {x: 1903.3, y: 40}, camera: {left: 1870, down: 159}, viewportY: 415.86522601544857,
        targetDirection: "left"},
    {name: "peak", seed: 11, anchorX: 320, normalized: 0.5618129291338846,
        pose: {x: 303.3, y: 56}, camera: {left: 270, down: 319}, viewportY: 599.4397253543139,
        targetDirection: "left"},
    {name: "canyon", seed: 41, anchorX: 2560, normalized: 0.180077174003236,
        pose: {x: 2543.3, y: 32}, camera: {left: 2510, down: 79}, viewportY: 603.750608637929,
        targetDirection: "left"},
];
const windows = reliefVectors.map((witness) => {
    let relief = createRun({seed: witness.seed, reducedMotion: true});
    relief = updateRetention({...relief, pose: {...relief.pose, ...witness.pose,
        vx: 0, vy: 0, angle: 0, angularVelocity: 0}});
    controller.model = relief;
    controller.render();
    const camera = cameraForPose(relief.pose);
    const terrain = document.querySelector(".terrain-surface");
    const terrainPath = terrain.getAttribute("d");
    const anchorWorldY = terrainHeightAt(witness.seed, witness.anchorX);
    const landerBounds = rect(document.querySelector("#mission-lander"));
    const cueBounds = rect(document.querySelector("#next-site-cue"));
    const hudBounds = rect(document.querySelector("#lander-fuel-gauge"));
    const actionBounds = rect(document.querySelector("#lander-exit"));
    const currentScene = rect(scene);
    const currentStage = rect(stage);
    const currentRail = rect(rail);
    return {name: witness.name, camera, expectedCamera: witness.camera,
        normalized: terrainNormalizedHeightAt(witness.seed, witness.anchorX),
        expectedNormalized: witness.normalized,
        anchorSceneX: worldViewportX(witness.anchorX, camera),
        anchorViewportY: worldViewportY(anchorWorldY, camera), expectedViewportY: witness.viewportY,
        terrainParity: terrainPath === terrainSurfacePath(relief.terrainVertices),
        terrainVertex: relief.terrainVertices.some(([x, y]) => x === witness.anchorX && y === anchorWorldY),
        landerInside: landerBounds.left >= currentScene.left - .5 && landerBounds.right <= currentScene.right + .5 &&
            landerBounds.top >= currentScene.top - .5 && landerBounds.bottom <= currentScene.bottom + .5,
        targetDirection: root.dataset.targetDirection, expectedTargetDirection: witness.targetDirection,
        cueVisible: cueBounds.width > 0 && cueBounds.height > 0 && cueBounds.left >= currentScene.left - .5 &&
            cueBounds.right <= currentScene.right + .5 && cueBounds.top >= currentScene.top - .5 &&
            cueBounds.bottom <= currentScene.bottom + .5,
        geometry: {cue: cueBounds, hud: hudBounds, lander: landerBounds, action: actionBounds,
            overlaps: pairwiseOverlaps([["cue", cueBounds], ["hud", hudBounds],
                ["lander", landerBounds], ["action", actionBounds]])},
        stageRailSeparated: currentStage.bottom <= currentRail.top,
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth};
});
const zero = {left: 0, right: 0};
const collective = {left: 0.72, right: 0.72};
let warmup = updateRetention(createRun({seed: 0x12345678, reducedMotion: true}));
const warmupTarget = warmup.retainedSites.find((candidate) => candidate.id === warmup.targetSiteId);
warmup = {...warmup, pose: {x: warmupTarget.center, y: warmupTarget.platformTop + .001,
    vx: 0, vy: -1, angle: 0, angularVelocity: 0}};
warmup = updateRetention(stepFlight(warmup, zero));
controller.model = warmup;
controller.render();
const generation = [];
const rendering = [];
let maximumDescendants = 0;
let maximumSites = 0;
let maximumChunks = 0;
let mission = updateRetention(createRun({seed: 0x12345678, reducedMotion: true}));
for (let completed = 0; completed < 100; completed += 1) {
    for (let step = 0; step < 90 && mission.state === "launching"; step += 1) {
        mission = updateRetention(stepFlight(mission, collective));
    }
    const target = mission.retainedSites.find((candidate) => candidate.id === mission.targetSiteId);
    mission = {...mission, pose: {x: target.center, y: target.platformTop + .001,
        vx: 0, vy: -1, angle: 0, angularVelocity: 0}};
    const generationStart = performance.now();
    mission = updateRetention(stepFlight(mission, zero));
    generation.push(performance.now() - generationStart);
    const renderStart = performance.now();
    controller.model = mission;
    controller.render();
    rendering.push(performance.now() - renderStart);
    maximumDescendants = Math.max(maximumDescendants, world.querySelectorAll("*").length);
    maximumSites = Math.max(maximumSites, mission.retainedSites.length);
    maximumChunks = Math.max(maximumChunks, mission.retainedChunks.length);
}
const percentile95 = (values) => [...values].sort((left, right) => left - right)[Math.ceil(.95 * values.length) - 1];
const hundredSites = {
    completed: mission.completedSites,
    generationP95: percentile95(generation),
    generationMaximum: Math.max(...generation),
    renderP95: percentile95(rendering),
    maximumDescendants,
    maximumSites,
    maximumChunks,
};
window.phase4p = {layoutWidth: innerWidth, opening, maximumWindow, explored, windows, hundredSites};
document.documentElement.dataset.phase4pReady = "true";
} catch (error) {
    window.phase4p = {error: String(error), stack: error.stack};
    document.documentElement.dataset.phase4pReady = "true";
}
"""


def browser_phase4p_contract(
    output: Path,
    width: int = 960,
    *,
    cpu_throttle_rate: int = 1,
) -> dict[str, object]:
    chromium = next(
        (candidate for name in ("google-chrome", "chromium", "chromium-browser") if (candidate := shutil.which(name))),
        None,
    )
    if chromium is None:
        raise AssertionError("Chromium or Google Chrome is required for Phase 4P browser contracts")
    page = output / "lander/index.html"
    probe_path = output / "phase4p-browser.js"
    source = page.read_text(encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(output)))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="phase4p-browser-server")
    profile = tempfile.TemporaryDirectory()
    process: subprocess.Popen[bytes] | None = None
    connection: DevToolsConnection | None = None
    try:
        probe_path.write_text(PROBE, encoding="utf-8")
        page.write_text(
            source.replace("</body>", '<script type="module" src="/phase4p-browser.js"></script></body>', 1),
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
        connection.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
        )
        if cpu_throttle_rate > 1:
            connection.call("Emulation.setCPUThrottlingRate", {"rate": cpu_throttle_rate})
        connection.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_address[1]}/lander/"})
        for _ in range(240):
            if connection.evaluate("document.documentElement?.dataset.phase4pReady === 'true'"):
                return connection.evaluate("phase4p")
            time.sleep(0.025)
        raise AssertionError("Phase 4P browser probe did not initialize")
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


class Phase4PBrowserTests(RepositoryFixture):
    def test_real_chromium_projects_canonical_relief_lattice_and_vertical_camera(self) -> None:
        output = self.build()
        results = [browser_phase4p_contract(output, width) for width in (960, 320)]
        for result, width in zip(results, (960, 320), strict=True):
            with self.subTest(width=width):
                self.assertNotIn("error", result, result)
                self.assertEqual(result["layoutWidth"], width)
                for foot in result["opening"]["visibleFeet"]:
                    self.assertTrue(foot["terrainVertex"])
                    self.assertTrue(foot["inside"])
                    self.assertAlmostEqual(foot["terrainX"], foot["scaffoldX"])
                    self.assertAlmostEqual(foot["terrainY"], foot["scaffoldY"])
                maximum = result["maximumWindow"]
                self.assertEqual(maximum["camera"], {"left": 0, "down": 319})
                self.assertTrue(maximum["terrainParity"])
                self.assertEqual(maximum["target"]["direction"], "none")
                self.assertEqual(maximum["target"]["cue"]["width"], 0)
                self.assertEqual(maximum["target"]["cue"]["height"], 0)
                for name in ("landingFace", "noc", "mast"):
                    self.assertGreater(maximum["target"][name]["width"], 0)
                    self.assertGreater(maximum["target"][name]["height"], 0)
                self.assertTrue(maximum["target"]["landingFaceInside"])
                self.assertTrue(maximum["target"]["nocInside"])
                self.assertTrue(maximum["target"]["mastInside"])
                self.assertEqual(len(maximum["supportFeet"]), 6)
                for foot in maximum["supportFeet"]:
                    self.assertTrue(foot["terrainVertex"])
                    self.assertTrue(foot["scaffoldEndpoint"])
                    self.assertTrue(foot["terrainClipped"])
                    self.assertTrue(foot["scaffoldClipped"])
                    self.assertAlmostEqual(foot["terrainX"], foot["scaffoldX"])
                    self.assertAlmostEqual(foot["terrainY"], foot["scaffoldY"])
                for witness in result["windows"]:
                    self.assertEqual(witness["camera"], witness["expectedCamera"])
                    self.assertAlmostEqual(witness["normalized"], witness["expectedNormalized"])
                    self.assertEqual(witness["anchorSceneX"], 500)
                    self.assertAlmostEqual(witness["anchorViewportY"], witness["expectedViewportY"])
                    self.assertTrue(witness["terrainParity"])
                    self.assertTrue(witness["terrainVertex"])
                    self.assertTrue(witness["landerInside"])
                    self.assertEqual(witness["targetDirection"], witness["expectedTargetDirection"])
                    self.assertTrue(witness["cueVisible"])
                    for name in ("cue", "hud", "lander", "action"):
                        self.assertGreater(witness["geometry"][name]["width"], 0)
                        self.assertGreater(witness["geometry"][name]["height"], 0)
                    for pair in witness["geometry"]["overlaps"]:
                        self.assertFalse(pair["overlaps"], {"witness": witness["name"], **pair})
                    self.assertTrue(witness["stageRailSeparated"])
                    self.assertEqual(witness["overflow"], 0)
        result = results[0]
        opening = result["opening"]
        self.assertEqual(opening["cameraX"], "0px")
        self.assertEqual(opening["cameraY"], "79px")
        self.assertEqual(opening["deckLevel"], 116)
        self.assertTrue(opening["staticTerrainParity"])
        self.assertTrue(opening["runtimeTerrainParity"])
        self.assertEqual(opening["scaffoldMembers"], opening["expectedMembers"])
        self.assertEqual(len(opening["feet"]), 6)
        explored = result["explored"]
        self.assertAlmostEqual(float(explored["cameraX"].removesuffix("px")), -10 * explored["expectedCamera"]["left"])
        self.assertAlmostEqual(float(explored["cameraY"].removesuffix("px")), explored["expectedCamera"]["down"])
        self.assertLessEqual(explored["descendants"], 80)
        self.assertIn(explored["targetDirection"], ("left", "right"))
        hundred_sites = result["hundredSites"]
        self.assertEqual(hundred_sites["completed"], 100)
        self.assertLessEqual(hundred_sites["maximumDescendants"], 80)
        self.assertLessEqual(hundred_sites["maximumSites"], 3)
        self.assertLessEqual(hundred_sites["maximumChunks"], 5)
        self.assertLess(hundred_sites["renderP95"], 4)
        self.assertLess(hundred_sites["generationP95"], 25)
        self.assertLess(hundred_sites["generationMaximum"], 50)
