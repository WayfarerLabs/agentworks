"""Real-Chromium witnesses for fixed-height Phase 4Q terrain presentation."""

from __future__ import annotations

import base64
import hashlib
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from functools import partial
from http.server import ThreadingHTTPServer
from math import ceil
from pathlib import Path

from chromium_test_support import DevToolsConnection, acquire_chromium, cleanup_profile, stop_process
from lander_chromium_phase4k import _QuietHandler
from site_test_support import RepositoryFixture

PROBE = r"""
import { landerGameController as controller } from "/static/lander-game.js";
import { advanceMissionSequence, classifySweptContact, createRun, stepFlight, updateRetention } from "/static/lander-model.js";
import { MAX_NORMALIZED_DECK, STATIC_WORLD_SEED, TERRAIN_PROFILES, WORLD_MAX_X, WORLD_MIN_X,
    createSiteForIndex, hullForPose, normalizeDegrees, terrainHeightAt } from "/static/lander-world.js";

if (controller.frameId !== null) cancelAnimationFrame(controller.frameId);
controller.frameId = null;
controller.paused = true;
const root = document.querySelector("#lander-game");
const main = document.querySelector("main");
const shell = document.querySelector("#lander-scene-shell");
const stage = document.querySelector("#lander-scene-stage");
const rail = document.querySelector("#lander-controls-rail");
const world = document.querySelector("#lander-world");
const restart = document.querySelector("#lander-restart");
const exit = document.querySelector("#lander-exit");
const zero = {left: 0, right: 0};

const box = (node) => {
    const rect = node.getBoundingClientRect();
    return {left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
        width: rect.width, height: rect.height, clientWidth: node.clientWidth ?? rect.width,
        scrollWidth: node.scrollWidth ?? rect.width, clientHeight: node.clientHeight ?? rect.height,
        scrollHeight: node.scrollHeight ?? rect.height};
};
const landmarkNodes = () => [...document.querySelectorAll(
    ".site-header, .site-header *, .site-footer, .site-footer *, " +
    "#lander-start, #lander-start *, #lander-restart, #lander-restart *, #lander-exit, #lander-exit *")];
const snapshot = (label = "") => ({
    label,
    state: controller.model.state,
    failureCause: controller.model.failureCause,
    fuel: controller.model.fuel,
    pose: controller.model.pose,
    viewport: {innerWidth, innerHeight},
    document: {clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
        clientHeight: document.documentElement.clientHeight,
        scrollHeight: document.documentElement.scrollHeight,
        scrollLeft: document.documentElement.scrollLeft,
        scrollTop: document.documentElement.scrollTop},
    body: box(document.body), main: box(main), game: box(root), shell: box(shell),
    stage: box(stage), rail: box(rail), restart: box(restart), exit: box(exit),
    header: box(document.querySelector(".site-header")),
    footer: box(document.querySelector(".site-footer")),
    footerNav: box(document.querySelector(".site-footer nav")),
    landmarks: landmarkNodes().map((node) => ({tag: node.tagName, id: node.id,
        href: node.getAttribute("href"), hidden: node.closest("[hidden]") !== null, box: box(node)})),
    worldTransform: getComputedStyle(world).transform,
    stageRatio: stage.getBoundingClientRect().width / stage.getBoundingClientRect().height,
});
const useSeed = (seed, x = 36, y = 30, label = "seed") => {
    const model = createRun({seed, reducedMotion: true});
    controller.model = updateRetention({...model, pose: {...model.pose, x, y}});
    controller.render();
    return snapshot(label);
};
const service = () => {
    let model = createRun({seed: 11, reducedMotion: false});
    const target = model.retainedSites[0];
    model = stepFlight({...model, pose: {x: target.center, y: target.platformTop + .001,
        vx: 0, vy: -1, angle: 0, angularVelocity: 0}}, zero);
    controller.model = updateRetention(model); controller.render();
    const landed = snapshot("service-landed");
    model = advanceMissionSequence(model, .3, false);
    model = advanceMissionSequence(model, .9, false);
    model = advanceMissionSequence(model, 1.4, false);
    controller.model = updateRetention(model); controller.render();
    return [landed, snapshot("service-powered")];
};
const fail = () => {
    let model = createRun({seed: 41, reducedMotion: true});
    model = stepFlight({...model, pose: {...model.pose, x: 70, y: terrainHeightAt(41, 70) - 1, vy: -10}}, zero);
    controller.model = updateRetention(model);
    controller.render();
    return snapshot("crash");
};
const ballistic = () => {
    let model = createRun({seed: 41, reducedMotion: true});
    const target = model.retainedSites.find((site) => site.id === model.targetSiteId);
    model = stepFlight({...model, fuel: 0, pose: {x: target.center + 125, y: 70,
        vx: 500, vy: 10, angle: 0, angularVelocity: 0}}, {left: .72, right: .72});
    controller.model = updateRetention(model); controller.render();
    const clear = snapshot("past-target-ballistic");
    for (let step = 0; step < 2000 && model.state === "flying"; step += 1) {
        model = stepFlight(model, zero);
    }
    controller.model = updateRetention(model); controller.render();
    return [clear, snapshot("ballistic-contact")];
};
const edge = (direction) => {
    const boundary = direction > 0 ? WORLD_MAX_X : WORLD_MIN_X;
    let model = createRun({seed: 11, reducedMotion: true});
    model = updateRetention({...model, pose: {...model.pose, x: boundary - direction * 10, y: 50,
        vx: direction * 2000, vy: 0}});
    controller.model = model; controller.render();
    const visible = snapshot(direction > 0 ? "right-terminus-visible" : "left-terminus-visible");
    model = stepFlight(model, zero);
    controller.model = updateRetention(model); controller.render();
    return [visible, snapshot(direction > 0 ? "right-terminus-contact" : "left-terminus-contact")];
};
const retry = () => { restart.click(); return snapshot("retry"); };
const leave = () => { exit.click(); return snapshot("exit"); };
const focusFooter = () => { document.querySelector(".footer-game-link").focus({preventScroll: true});
    return snapshot("focus-footer"); };
const timingSummary = (values) => {
    const ordered = values.toSorted((left, right) => left - right);
    return {samples: ordered.length, p95: ordered[Math.ceil(ordered.length * .95) - 1],
        maximum: ordered.at(-1)};
};
const terrainContract = () => {
    const normalized = Object.values(TERRAIN_PROFILES).flat();
    const sites = [];
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        for (let index = -4095; index <= 4095; index += 1) sites.push(createSiteForIndex(seed, index));
    }
    const spacings = sites.slice(1).flatMap((site, index) =>
        site.seed === sites[index].seed ? [site.center - sites[index].center] : []);
    return {minimum: Math.min(...normalized), maximum: Math.max(...normalized),
        maximumDeck: Math.max(...sites.map((site) => site.normalizedDeck)),
        maximumOrdinal: Math.max(...sites.map((site) => site.candidateOrdinal)),
        minimumSpacing: Math.min(...spacings), maximumSpacing: Math.max(...spacings),
        deckLimit: MAX_NORMALIZED_DECK};
};
const maximumKnotSweep = (direction) => {
    const model = {...createRun({seed: 41}), retainedSites: [], targetSiteId: null,
        terrainAuthority: "context", terrainVertices: []};
    const previous = {x: 0, y: 35.8, vx: 0, vy: 0,
        angle: direction * .9, angularVelocity: 0};
    const angularTravel = direction * 73091.33333333333;
    const next = {...previous, x: direction * 11746.828095238097,
        angle: normalizeDegrees(previous.angle + angularTravel)};
    const corner = hullForPose({...next, angle: previous.angle + angularTravel})
        .reduce((selected, candidate) => direction * candidate.x > direction * selected.x ? candidate : selected);
    const edgeX = corner.x - direction * .001;
    const features = [{cause: "terminus", priority: 3,
        segment: [{x: edgeX, y: 20}, {x: edgeX, y: 50}]}];
    const staleInstrumentation = {};
    const staleContact = classifySweptContact(model, previous, next,
        {angularTravel: direction * 53148, instrumentation: staleInstrumentation, features});
    if (staleContact !== null || staleInstrumentation.visitedKnots !== 53150)
        throw new Error("maximum knot sweep final slabs are not authoritative");
    const classify = () => {
        const instrumentation = {};
        const contact = classifySweptContact(model, previous, next, {angularTravel, instrumentation, features});
        if (contact?.cause !== "terminus" || contact.kind !== "unsafe" || contact.time <= .9999 ||
            instrumentation.visitedKnots !== 73094 || instrumentation.maxKnotHulls > 2 ||
            instrumentation.maxStack > 20 || instrumentation.prunedSlabs <= 50000 ||
            instrumentation.constructedKnotHulls >= 256) throw new Error("maximum knot sweep contract drifted");
        return instrumentation;
    };
    classify();
    const values = [];
    let instrumentation;
    for (let repetition = 0; repetition < 10; repetition += 1) {
        const started = performance.now(); instrumentation = classify(); values.push(performance.now() - started);
    }
    return {direction, timing: timingSummary(values), instrumentation, staleInstrumentation};
};
const maximumKnotWitness = () => {
    const beforeDom = domCounts(); const beforeCleanup = controller.cleanups.length;
    const directions = [-1, 1].map(maximumKnotSweep);
    return {directions, beforeDom, afterDom: domCounts(), beforeCleanup,
        afterCleanup: controller.cleanups.length};
};
const domCounts = () => ({document: document.querySelectorAll("*").length,
    world: world.querySelectorAll("*").length, worldChildren: world.children.length,
    terrainPaths: document.querySelectorAll("#terrain-layer > path").length,
    siteGroups: document.querySelectorAll("#site-layer > .lander-site").length});
const recordRender = (values, states, maxima) => {
    const started = performance.now(); controller.render(); values.push(performance.now() - started);
    states[controller.model.state] = (states[controller.model.state] ?? 0) + 1;
    const counts = domCounts();
    maxima.document = Math.max(maxima.document, counts.document);
    maxima.world = Math.max(maxima.world, counts.world);
};
const clearLaunch = (startingModel) => {
    let model = startingModel;
    for (let step = 0; step < 90 && model.state === "launching"; step += 1) {
        model = updateRetention(stepFlight(model, {left: .72, right: .72}));
    }
    return model;
};
const serviceTarget = (model) => {
    const target = model.retainedSites.find((site) => site.id === model.targetSiteId);
    const contact = {...model, pose: {x: target.center, y: target.platformTop + .001,
        vx: 0, vy: -1, angle: 0, angularVelocity: 0}};
    return updateRetention(stepFlight(contact, zero));
};
const warmup = (seed) => {
    let model = updateRetention(createRun({seed, reducedMotion: true}));
    let directAllowances = 0;
    let poweredCheckpoints = 0;
    controller.model = model;
    controller.render();
    for (let completed = 0; completed < 12; completed += 1) {
        if (model.state === "launching") {
            model = clearLaunch(model);
            controller.model = model;
            controller.render();
        }
        model = serviceTarget(model);
        if (model.targetSiteId !== null) directAllowances += 1;
        const active = model.retainedSites.find((site) => site.id === model.activeSiteId);
        if (active?.powered && active.nocStage === 7 && model.checkpoint) poweredCheckpoints += 1;
        controller.model = model;
        controller.render();
    }
    return {completedSites: model.completedSites, directAllowances, poweredCheckpoints};
};
const longevity = (seed) => {
    const warmupResult = warmup(seed);
    const generationValues = [];
    const frameValues = [];
    const frameStates = {};
    const maxima = {sites: 0, chunks: 0, terrainVertices: 0, document: 0, world: 0};
    let directAllowances = 0;
    let poweredCheckpoints = 0;
    let stabilizedDom = null;
    let model = updateRetention(createRun({seed, reducedMotion: true}));
    controller.model = model;
    recordRender(frameValues, frameStates, maxima);
    for (let completed = 0; completed < 100; completed += 1) {
        if (model.state === "launching") {
            model = clearLaunch(model);
            controller.model = model;
            recordRender(frameValues, frameStates, maxima);
        }
        const started = performance.now();
        model = serviceTarget(model);
        generationValues.push(performance.now() - started);
        if (model.targetSiteId !== null) directAllowances += 1;
        const active = model.retainedSites.find((site) => site.id === model.activeSiteId);
        if (active?.powered && active.nocStage === 7 && model.checkpoint) poweredCheckpoints += 1;
        maxima.sites = Math.max(maxima.sites, model.retainedSites.length);
        maxima.chunks = Math.max(maxima.chunks, model.retainedChunks.length);
        maxima.terrainVertices = Math.max(maxima.terrainVertices, model.terrainVertices.length);
        controller.model = model;
        recordRender(frameValues, frameStates, maxima);
        if (completed === 1) stabilizedDom = domCounts();
    }
    return {seed, warmup: warmupResult, completedSites: model.completedSites,
        finalState: model.state, directAllowances,
        poweredCheckpoints,
        generation: timingSummary(generationValues),
        frames: timingSummary(frameValues), frameStates,
        maxima, stabilizedDom, finalDom: domCounts(), cleanupCount: controller.cleanups.length};
};
const valleyX = (seed) => Array.from({length: 33}, (_, index) => index * 16)
    .reduce((lowest, x) => terrainHeightAt(seed, x) < terrainHeightAt(seed, lowest) ? x : lowest, 0);
const normalizedAt = (seed, x) => (terrainHeightAt(seed, x) + 9.2) / 64;
window.phase4q = {snapshot, useSeed, service, fail, ballistic, edge, retry, leave, focusFooter, longevity,
    maximumKnotWitness,
    normalizedAt, terrainContract, valleyX, STATIC_WORLD_SEED,
    listenerTargets: {motion: controller.motion}};
document.documentElement.dataset.phase4qReady = "true";
"""

LISTENER_TARGETS = {
    "window": "window",
    "document": "document",
    "start": "document.querySelector('#lander-start')",
    "exit": "document.querySelector('#lander-exit')",
    "restart": "document.querySelector('#lander-restart')",
    "shell": "document.querySelector('#lander-scene-shell')",
    "stage": "document.querySelector('#lander-scene-stage')",
    "motion": "phase4q.listenerTargets.motion",
}


def _listener_counts(connection: DevToolsConnection) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for name, expression in LISTENER_TARGETS.items():
        evaluated = connection.call("Runtime.evaluate", {"expression": expression})
        object_id = evaluated["result"].get("objectId")
        if object_id is None:
            raise AssertionError(f"Chromium listener target {name} has no remote object")
        listeners = connection.call("DOMDebugger.getEventListeners", {"objectId": object_id})["listeners"]
        by_type: dict[str, int] = {}
        for listener in listeners:
            event_type = listener["type"]
            by_type[event_type] = by_type.get(event_type, 0) + 1
        counts[name] = by_type
        connection.call("Runtime.releaseObject", {"objectId": object_id})
    return counts


def browser_phase4q_contract(
    output: Path,
    probe_source: str = PROBE,
    *,
    chromium_arguments: tuple[str, ...] = (),
    cpu_throttling_rate: float = 1,
) -> dict[str, object]:
    chromium = next(
        (candidate for name in ("google-chrome", "chromium", "chromium-browser")
         if (candidate := shutil.which(name))),
        None,
    )
    if chromium is None:
        raise AssertionError("Chromium or Google Chrome is required for Phase 4Q browser contracts")
    page = output / "lander/index.html"
    probe_path = output / "phase4q-browser.js"
    source = page.read_text(encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(output)))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="phase4q-browser-server")
    profile: tempfile.TemporaryDirectory[str] | None = None
    process: subprocess.Popen[bytes] | None = None
    connection: DevToolsConnection | None = None
    try:
        probe_path.write_text(probe_source, encoding="utf-8")
        page.write_text(
            source.replace("</body>", '<script type="module" src="/phase4q-browser.js"></script></body>', 1),
            encoding="utf-8",
        )
        thread.start()
        profile, process, connection = acquire_chromium(
            chromium, extra_arguments=chromium_arguments
        )
        for domain in ("Runtime", "Page"):
            connection.call(f"{domain}.enable")
        connection.call("Emulation.setCPUThrottlingRate", {"rate": cpu_throttling_rate})
        connection.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_address[1]}/lander/"})
        for _ in range(240):
            if connection.evaluate("document.documentElement?.dataset.phase4qReady === 'true'"):
                break
            time.sleep(0.025)
        else:
            raise AssertionError("Phase 4Q browser probe did not initialize")

        screenshot_directory = output / "_phase4q-screenshots"
        screenshot_directory.mkdir(exist_ok=True)
        result: dict[str, object] = {"viewports": {}, "screenshots": [], "longevity": [],
                                    "listeners": [], "maximumKnot": None,
                                    "terrain": connection.evaluate("phase4q.terrainContract()")}
        viewports = (
            ("narrow", 320, 780, False),
            ("zoomEquivalent", 320, 240, False),
            ("touchLandscape", 667, 320, True),
            ("desktop", 1000, 780, False),
        )
        for name, width, height, touch in viewports:
            connection.call("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": height, "deviceScaleFactor": 1, "mobile": touch,
            })
            connection.call("Emulation.setTouchEmulationEnabled", {"enabled": touch, "maxTouchPoints": 5})
            time.sleep(0.05)
            stages = [connection.evaluate("phase4q.snapshot('preflight')")]
            stages.append(connection.evaluate("phase4q.useSeed(11, 36, 30, 'seed11-summit')"))
            stages.append(connection.evaluate(
                "phase4q.useSeed(11, phase4q.valleyX(11), 2.5, 'seed11-low-valley')"
            ))
            stages.append(connection.evaluate("phase4q.useSeed(41, 140, 30, 'seed41-summit')"))
            stages.append(connection.evaluate(
                "phase4q.useSeed(phase4q.STATIC_WORLD_SEED, phase4q.valleyX(phase4q.STATIC_WORLD_SEED), "
                "3, 'static-valley')"
            ))
            stages.append(connection.evaluate("phase4q.useSeed(11, 36, 55, 'ceiling-flight')"))
            stages.append(connection.evaluate("phase4q.useSeed(41, -40, 30, 'reverse-left')"))
            stages.append(connection.evaluate("phase4q.useSeed(41, 160, 30, 'reverse-right')"))
            stages.extend(connection.evaluate("phase4q.service()"))
            stages.extend(connection.evaluate("phase4q.ballistic()"))
            stages.extend(connection.evaluate("phase4q.edge(-1)"))
            stages.extend(connection.evaluate("phase4q.edge(1)"))
            stages.append(connection.evaluate("phase4q.fail()"))
            stages.append(connection.evaluate("phase4q.retry()"))
            stages.append(connection.evaluate("phase4q.focusFooter()"))
            connection.call("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": width // 2, "y": height // 2, "deltaX": 0, "deltaY": 180,
            })
            stages.append(connection.evaluate("phase4q.snapshot('wheel')"))
            if touch:
                connection.call("Input.dispatchTouchEvent", {
                    "type": "touchStart", "touchPoints": [{"x": width // 2, "y": height // 2}],
                })
                connection.call("Input.dispatchTouchEvent", {"type": "touchCancel", "touchPoints": []})
                stages.append(connection.evaluate("phase4q.snapshot('touch')"))
            stages.append(connection.evaluate("phase4q.leave()"))
            result["viewports"][name] = stages
            for seed, pose_x, sample_x in (
                (11, 55, 55),
                (41, 55, 80),
                (1095194417, 55, 55),
            ):
                connection.evaluate(f"phase4q.useSeed({seed}, {pose_x}, 30, 'screenshot-{seed}')")
                png = base64.b64decode(connection.call("Page.captureScreenshot", {
                    "format": "png", "captureBeyondViewport": False,
                })["data"])
                screenshot = screenshot_directory / f"phase4q-{seed}-{width}x{height}.png"
                screenshot.write_bytes(png)
                png_width, png_height = struct.unpack(">II", png[16:24])
                result["screenshots"].append({
                    "seed": seed, "width": png_width, "height": png_height,
                    "peakNormalized": connection.evaluate(f"phase4q.normalizedAt({seed}, {sample_x})"),
                    "bytes": len(png), "sha256": hashlib.sha256(png).hexdigest(),
                    "path": str(screenshot),
                })
            connection.evaluate("phase4q.leave()")
            if name == "desktop":
                result["listeners"].append(_listener_counts(connection))
                for seed in (11, 39, 41, 1095194417):
                    result["longevity"].append(connection.evaluate(f"phase4q.longevity({seed})"))
                    result["listeners"].append(_listener_counts(connection))
                result["maximumKnot"] = connection.evaluate("phase4q.maximumKnotWitness()")
                result["listeners"].append(_listener_counts(connection))
        return result
    finally:
        if connection is not None:
            connection.close()
        if process is not None:
            stop_process(process)
        server.shutdown()
        server.server_close()
        if thread.is_alive():
            thread.join(timeout=5)
        page.write_text(source, encoding="utf-8")
        probe_path.unlink(missing_ok=True)
        if profile is not None:
            cleanup_profile(profile)


class Phase4QBrowserTests(RepositoryFixture):
    def assert_longevity_contract(self, result: dict[str, object]) -> None:
        witnesses = result["longevity"]
        self.assertEqual([witness["seed"] for witness in witnesses], [11, 39, 41, 1095194417])
        listener_snapshots = result["listeners"]
        self.assertEqual(len(listener_snapshots), 6)
        self.assertTrue(all(snapshot == listener_snapshots[0] for snapshot in listener_snapshots))
        self.assertEqual(listener_snapshots[0], {
            "window": {"blur": 1, "resize": 1},
            "document": {"keydown": 1, "keyup": 1, "visibilitychange": 1},
            "start": {"click": 1},
            "exit": {"click": 1},
            "restart": {"click": 1},
            "shell": {"focusout": 1},
            "stage": {event: 1 for event in (
                "pointerdown", "pointermove", "pointerup", "pointercancel", "lostpointercapture"
            )},
            "motion": {"change": 1},
        })
        listener_count = sum(
            sum(types.values()) for types in listener_snapshots[0].values()
        )
        for witness in witnesses:
            with self.subTest(seed=witness["seed"], contract="longevity"):
                self.assertEqual(witness["warmup"], {
                    "completedSites": 12,
                    "directAllowances": 12,
                    "poweredCheckpoints": 12,
                })
                self.assertEqual(witness["completedSites"], 100)
                self.assertEqual(witness["finalState"], "launching")
                self.assertEqual(witness["directAllowances"], 100)
                self.assertEqual(witness["poweredCheckpoints"], 100)
                self.assertEqual(witness["generation"]["samples"], 100)
                self.assertLess(witness["generation"]["p95"], 25)
                self.assertGreaterEqual(
                    witness["generation"]["maximum"], witness["generation"]["p95"]
                )
                self.assertEqual(witness["frames"]["samples"], 200)
                self.assertEqual(witness["frameStates"], {"flying": 100, "launching": 100})
                self.assertLess(witness["frames"]["p95"], 4)
                self.assertGreaterEqual(witness["frames"]["maximum"], witness["frames"]["p95"])
                self.assertEqual(witness["maxima"]["sites"], 3)
                self.assertLessEqual(witness["maxima"]["chunks"], 5)
                self.assertLessEqual(witness["maxima"]["terrainVertices"], 48)
                self.assertLessEqual(witness["maxima"]["world"], 76)
                self.assertEqual(witness["maxima"]["document"], witness["finalDom"]["document"])
                self.assertEqual(witness["maxima"]["world"], witness["finalDom"]["world"])
                self.assertEqual(witness["stabilizedDom"], witness["finalDom"])
                self.assertEqual(witness["finalDom"]["worldChildren"], 5)
                self.assertEqual(witness["finalDom"]["terrainPaths"], 3)
                self.assertEqual(witness["finalDom"]["siteGroups"], 3)
                self.assertEqual(witness["cleanupCount"], listener_count + 1)

        maximum_knot = result["maximumKnot"]
        self.assertEqual(maximum_knot["beforeDom"], maximum_knot["afterDom"])
        self.assertEqual(maximum_knot["beforeCleanup"], maximum_knot["afterCleanup"])
        self.assertEqual([row["direction"] for row in maximum_knot["directions"]], [-1, 1])
        for row in maximum_knot["directions"]:
            self.assertEqual(row["timing"]["samples"], 10)
            self.assertLess(row["timing"]["p95"], 100)
            self.assertGreaterEqual(row["timing"]["maximum"], row["timing"]["p95"])
            self.assertEqual(row["instrumentation"]["visitedKnots"], 73094)
            self.assertEqual(row["staleInstrumentation"]["visitedKnots"], 53150)
            self.assertEqual(
                row["instrumentation"]["visitedKnots"] - row["staleInstrumentation"]["visitedKnots"],
                19944,
            )
            self.assertLessEqual(row["instrumentation"]["maxKnotHulls"], 2)
            self.assertLessEqual(row["instrumentation"]["maxStack"], 20)
            self.assertGreater(row["instrumentation"]["prunedSlabs"], 50000)
            self.assertLess(row["instrumentation"]["constructedKnotHulls"], 256)

    def test_fixed_scene_has_no_page_growth_or_scroll_across_lifecycle(self) -> None:
        result = browser_phase4q_contract(self.build())
        terrain = result["terrain"]
        self.assertEqual(terrain["minimum"], 0.1)
        self.assertEqual(terrain["maximum"], 0.6)
        self.assertEqual(round(terrain["maximumDeck"], 7), 0.4969375)
        self.assertLessEqual(terrain["maximumDeck"], terrain["deckLimit"])
        self.assertEqual(terrain["maximumOrdinal"], 5)
        self.assertEqual([terrain["minimumSpacing"], terrain["maximumSpacing"]], [152, 232])
        expected = {
            "narrow": (320, 780),
            "zoomEquivalent": (320, 240),
            "touchLandscape": (667, 320),
            "desktop": (1000, 780),
        }
        expected_labels = [
            "preflight", "seed11-summit", "seed11-low-valley", "seed41-summit", "static-valley",
            "ceiling-flight", "reverse-left", "reverse-right", "service-landed", "service-powered",
            "past-target-ballistic", "ballistic-contact", "left-terminus-visible", "left-terminus-contact",
            "right-terminus-visible", "right-terminus-contact", "crash", "retry", "focus-footer", "wheel",
        ]
        for name, stages in result["viewports"].items():
            width, height = expected[name]
            labels = expected_labels + (["touch"] if name == "touchLandscape" else []) + ["exit"]
            self.assertEqual([stage["label"] for stage in stages], labels)
            self.assertEqual(stages[0]["state"], "preflight")
            self.assertEqual(stages[8]["state"], "landed")
            self.assertEqual(stages[9]["state"], "launching")
            self.assertEqual(stages[10]["state"], "flying")
            self.assertEqual(stages[10]["fuel"], 0)
            self.assertGreater(stages[10]["pose"]["x"], 132)
            self.assertGreater(stages[10]["pose"]["y"], 56)
            self.assertEqual(stages[11]["state"], "failed")
            self.assertEqual(stages[11]["failureCause"], "terrain")
            self.assertEqual(stages[12]["state"], "flying")
            self.assertEqual(stages[13]["failureCause"], "terminus")
            self.assertEqual(stages[14]["state"], "flying")
            self.assertEqual(stages[15]["failureCause"], "terminus")
            self.assertEqual(stages[16]["state"], "failed")
            self.assertEqual(stages[17]["state"], "flying")
            self.assertEqual(stages[-1]["state"], "preflight")
            first_height = stages[0]["stage"]["height"]
            first_width = stages[0]["stage"]["width"]
            for stage in stages:
                with self.subTest(viewport=name, state=stage["state"]):
                    self.assertEqual(stage["viewport"], {"innerWidth": width, "innerHeight": height})
                    document = stage["document"]
                    self.assertEqual(document, {
                        "clientWidth": width, "scrollWidth": width,
                        "clientHeight": height, "scrollHeight": height,
                        "scrollLeft": 0, "scrollTop": 0,
                    })
                    for component in (
                        "body", "header", "main", "game", "shell", "stage", "rail", "footer", "footerNav"
                    ):
                        metrics = stage[component]
                        self.assertLessEqual(metrics["scrollWidth"], ceil(metrics["width"]) + 1)
                        self.assertLessEqual(metrics["scrollHeight"], ceil(metrics["height"]) + 1)
                        self.assertGreaterEqual(metrics["left"], 0)
                        self.assertGreaterEqual(metrics["top"], 0)
                        self.assertLessEqual(metrics["right"], width)
                        self.assertLessEqual(metrics["bottom"], height)
                    for landmark in stage["landmarks"]:
                        metrics = landmark["box"]
                        if not landmark["hidden"] and metrics["width"]:
                            self.assertGreaterEqual(metrics["left"], 0)
                            self.assertGreaterEqual(metrics["top"], 0)
                            self.assertLessEqual(metrics["right"], width)
                            self.assertLessEqual(metrics["bottom"], height)
                            self.assertLessEqual(metrics["scrollWidth"], ceil(metrics["width"]) + 1)
                            self.assertLessEqual(metrics["scrollHeight"], ceil(metrics["height"]) + 1)
                    self.assertAlmostEqual(stage["stageRatio"], 25 / 16, places=3)
                    self.assertAlmostEqual(stage["stage"]["width"], first_width, places=5)
                    self.assertAlmostEqual(stage["stage"]["height"], first_height, places=5)
                    transform = stage["worldTransform"]
                    self.assertTrue(transform == "none" or transform.endswith(", 0)"), transform)
                    for action in (stage["restart"], stage["exit"]):
                        if action["width"]:
                            self.assertGreaterEqual(action["width"], 44)
                            self.assertGreaterEqual(action["height"], 44)
            self.assertGreater(first_width, 0)
            self.assertGreater(first_height, 0)
        screenshots = result["screenshots"]
        self.assertEqual([item["seed"] for item in screenshots], [11, 41, 1095194417] * 4)
        self.assertEqual([(item["width"], item["height"]) for item in screenshots],
                         [(320, 780)] * 3 + [(320, 240)] * 3 + [(667, 320)] * 3 +
                         [(1000, 780)] * 3)
        self.assertTrue(all(item["bytes"] > 3_000 for item in screenshots))
        self.assertIn(0.6, [item["peakNormalized"] for item in screenshots])
        self.assertTrue(all(Path(item["path"]).is_file() for item in screenshots))
        self.assertEqual(len({item["sha256"] for item in screenshots}), 12)
        self.assert_longevity_contract(result)

    def test_longevity_witness_rejects_reduced_workload_mutation(self) -> None:
        mutation = PROBE.replace("completed < 100", "completed < 10", 1)
        self.assertNotEqual(mutation, PROBE)
        result = browser_phase4q_contract(self.build(), mutation)
        with self.assertRaises(AssertionError):
            self.assertEqual(result["longevity"][0]["completedSites"], 100)
