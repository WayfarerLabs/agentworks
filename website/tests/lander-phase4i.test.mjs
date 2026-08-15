import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";

import {
    SUCCESS_STATUS,
    TURN_DIFFERENTIAL,
    advanceSimulation,
    advanceMissionSequence,
    createSimulationClock,
    createRun,
    enqueueInputEdge,
    fuelGaugeLevel,
    stepFlight,
    transitionMission,
} from "../static/lander-model.js";
import { siteScaffoldMembers, siteStructure } from "../static/lander-world.js";
import { animationHarness, controllerClasses, controllerFixture } from "./lander-test-dom.mjs";

const ROOT = new URL("../", import.meta.url).pathname;

function linearSegments(path) {
    const tokens = path.match(/[MLHVZ]|-?\d+(?:\.\d+)?(?:e[+-]?\d+)?/gi) ?? [];
    const segments = []; let cursor = [0, 0]; let start = [0, 0]; let index = 0; let command = null;
    const line = (next) => { segments.push([...cursor, ...next]); cursor = next; };
    while (index < tokens.length) {
        if (/^[MLHVZ]$/i.test(tokens[index])) command = tokens[index++].toUpperCase();
        if (command === "M") { cursor = [Number(tokens[index++]), Number(tokens[index++])]; start = cursor; command = "L"; }
        else if (command === "L") line([Number(tokens[index++]), Number(tokens[index++])]);
        else if (command === "H") line([Number(tokens[index++]), cursor[1]]);
        else if (command === "V") line([cursor[0], Number(tokens[index++])]);
        else if (command === "Z") { line(start); command = null; }
        else throw new Error(`Unsupported path token ${tokens[index]}`);
    }
    return segments;
}

function expectedScaffold(site) {
    const x = (value) => Number((value * 10).toFixed(12));
    const y = (value) => 548 - value * 10;
    return siteScaffoldMembers(site).map(({ start, end }) =>
        [x(start[0]), y(start[1]), x(end[0]), y(end[1])]);
}

function withinCollider(segments, collider) {
    const tolerance = 1e-10;
    for (const [x1, y1, x2, y2] of segments) {
        const world = [[x1 / 10, (548 - y1) / 10], [x2 / 10, (548 - y2) / 10]];
        assert.ok(Math.min(...world.map(([x]) => x)) - 0.1 >= collider.left - tolerance);
        assert.ok(Math.max(...world.map(([x]) => x)) + 0.1 <= collider.right + tolerance);
        assert.ok(Math.min(...world.map(([, y]) => y)) - 0.1 >= collider.bottom - tolerance);
        assert.ok(Math.max(...world.map(([, y]) => y)) + 0.1 <= collider.top + tolerance);
    }
}

function serviceFirstSite(reducedMotion = true) {
    const model = firstSiteApproach(reducedMotion);
    return stepFlight(model, { left: 0, right: 0 });
}

function firstSiteApproach(reducedMotion = true) {
    const model = createRun({ seed: 1, reducedMotion });
    const target = model.retainedSites[0];
    return { ...model, pose: { x: target.center, y: target.platformTop + 0.001,
        vx: 0, vy: -1, angle: 0, angularVelocity: 0 } };
}

async function controllerAt(model, timestamp = performance.now()) {
    const { LanderGameController } = await controllerClasses();
    const animation = animationHarness();
    globalThis.requestAnimationFrame = (callback) => animation.request(callback);
    globalThis.cancelAnimationFrame = (id) => animation.cancel(id);
    globalThis.document.hidden = true;
    const { root, elements } = controllerFixture();
    const controller = new LanderGameController(root);
    controller.paused = false; controller.frameId = null; controller.model = model;
    controller.clock = createSimulationClock(timestamp); controller.previousFrame = timestamp;
    controller.render();
    return { animation, controller, elements, timestamp };
}

function keyEvent(controller, type, code, timestamp) {
    return { type, code, key: code === "Space" ? " " : code, target: controller.lander_scene_shell,
        timeStamp: timestamp, repeat: false, ctrlKey: false, altKey: false, metaKey: false, shiftKey: false,
        composedPath: () => [controller.lander_scene_shell], preventDefault() {} };
}

function pointerEvent(controller, type, pointerId, timestamp) {
    return { type, pointerId, timeStamp: timestamp, isPrimary: true, button: 0, clientX: 300, clientY: 200,
        composedPath: () => [controller.lander_scene_shell], preventDefault() {} };
}

test("fuel-reference gauge is exact, unbounded, and restored with the checkpoint", () => {
    const run = createRun({ seed: 1 });
    assert.equal(run.fuel, 15); assert.equal(run.fuelGaugeReference, 30); assert.equal(fuelGaugeLevel(run), 0.5);
    assert.equal(fuelGaugeLevel({ ...run, fuel: 37.5, fuelGaugeReference: 50 }), 0.75);
    assert.equal(fuelGaugeLevel({ ...run, fuel: 75, fuelGaugeReference: 50 }), 1);
    assert.equal(fuelGaugeLevel({ ...run, fuel: 5, fuelGaugeReference: 0 }), 0);
    const ready = serviceFirstSite();
    assert.equal(ready.fuel, ready.fuelGaugeReference); assert.equal(fuelGaugeLevel(ready), 1);
    const crashed = { ...ready, state: "failed", fuel: ready.fuel - 5 };
    const restored = transitionMission(crashed, "RESTART");
    assert.equal(restored.fuel, ready.checkpoint.fuel);
    assert.equal(restored.fuelGaugeReference, ready.checkpoint.fuelGaugeReference);
    assert.equal(fuelGaugeLevel(restored), 1);
});

test("launch-ready holds indefinitely and the first effective request integrates in place", () => {
    let ready = serviceFirstSite();
    const original = structuredClone({ pose: ready.pose, fuel: ready.fuel,
        missionSeconds: ready.missionSeconds, sequenceSeconds: ready.sequenceSeconds });
    for (let index = 0; index < 1200; index += 1) {
        ready = stepFlight(ready, index % 2 ? { left: 0, right: 0 } : { left: 0, right: TURN_DIFFERENTIAL });
    }
    assert.deepEqual({ pose: ready.pose, fuel: ready.fuel, missionSeconds: ready.missionSeconds,
        sequenceSeconds: ready.sequenceSeconds }, original);
    assert.equal(ready.launchStarted, false); assert.equal(ready.status, SUCCESS_STATUS);
    const departed = stepFlight(ready, { left: 0.72, right: 0.72 });
    assert.equal(departed.launchStarted, true); assert.equal(departed.status, "");
    assert.ok(departed.fuel < ready.fuel); assert.ok(departed.pose.y > ready.pose.y);
    assert.ok(departed.missionSeconds > ready.missionSeconds);
    const released = stepFlight(departed, { left: 0, right: 0 });
    assert.ok(released.pose.vy < departed.pose.vy, "released launch receives ordinary gravity");
});

test("reduced-motion touchdown consumes held input until a fresh post-deployment edge", () => {
    const authorities = [
        { name: "keyboard", physical: { heldCodes: ["Space"], pointer: { active: false } } },
        { name: "pointer", token: 7, physical: { heldCodes: [],
            pointer: { active: true, id: 7, anchorX: 100, currentX: 100, token: 7 } } },
    ];
    for (const authority of authorities) {
        let clock = enqueueInputEdge(createSimulationClock(0), {
            timestamp: 0, left: 0.72, right: 0.72, token: authority.token, physical: authority.physical,
        });
        const touchdown = advanceSimulation(clock, firstSiteApproach(), 50);
        const ready = touchdown.model;
        assert.equal(touchdown.steps, 6, authority.name);
        assert.equal(ready.state, "launching"); assert.equal(ready.launchStarted, false);
        assert.equal(ready.status, SUCCESS_STATUS);
        assert.deepEqual(ready.commanded, { left: 0, right: 0, vectorAngle: 0 });
        assert.deepEqual(ready.pose, ready.checkpoint.pose); assert.equal(ready.fuel, ready.checkpoint.fuel);
        assert.deepEqual(touchdown.clock.input, { left: 0, right: 0, vectorAngle: 0 });
        assert.equal(touchdown.clock.queue.length, 0);
        const held = advanceSimulation(touchdown.clock, ready, 100);
        assert.equal(held.model.launchStarted, false); assert.equal(held.model.status, SUCCESS_STATUS);
        assert.deepEqual(held.model.pose, ready.pose); assert.equal(held.model.fuel, ready.fuel);
        clock = enqueueInputEdge(held.clock, { timestamp: 100.1, left: 0.72, right: 0.72,
            token: authority.token, physical: authority.physical });
        const fresh = advanceSimulation(clock, held.model, 100 + 1000 / 120);
        assert.equal(fresh.model.launchStarted, true); assert.equal(fresh.model.status, "");
        assert.ok(fresh.model.fuel < held.model.fuel); assert.ok(fresh.model.pose.y > held.model.pose.y);
    }
});

test("controller tears down the held keyboard and captured pointer at atomic launch-ready touchdown", async () => {
    for (const authority of ["keyboard", "pointer"]) {
        const fixture = await controllerAt(firstSiteApproach(), performance.now());
        const { animation, controller, timestamp } = fixture;
        if (authority === "keyboard") controller.onKeyDown(keyEvent(controller, "keydown", "Space", timestamp + 0.1));
        else controller.onPointer(pointerEvent(controller, "pointerdown", 7, timestamp + 0.1));
        animation.step(timestamp + 50);
        assert.equal(controller.model.state, "launching"); assert.equal(controller.model.launchStarted, false);
        assert.equal(controller.model.status, SUCCESS_STATUS); assert.deepEqual(controller.model.pose, controller.model.checkpoint.pose);
        assert.equal(controller.model.fuel, controller.model.checkpoint.fuel);
        assert.deepEqual(controller.model.commanded, { left: 0, right: 0, vectorAngle: 0 });
        assert.deepEqual(controller.clock.input, { left: 0, right: 0, vectorAngle: 0 });
        assert.equal(controller.clock.queue.length, 0); assert.equal(controller.frameId, null); assert.equal(animation.pending, 0);
        assert.equal(controller.heldKeys.size, 0); assert.equal(controller.pointer, null);
        assert.equal(controller.lander_scene_stage.hasPointerCapture(7), false);
        if (authority === "keyboard") controller.onKeyUp(keyEvent(controller, "keyup", "Space", timestamp + 60));
        else {
            controller.onPointer(pointerEvent(controller, "pointermove", 7, timestamp + 60));
            controller.onPointer(pointerEvent(controller, "pointerup", 7, timestamp + 61));
        }
        assert.equal(controller.model.launchStarted, false); assert.equal(animation.pending, 0);
        if (authority === "keyboard") controller.onKeyDown(keyEvent(controller, "keydown", "Space", timestamp + 70));
        else controller.onPointer(pointerEvent(controller, "pointerdown", 8, timestamp + 70));
        assert.equal(animation.pending, 1);
        animation.step(timestamp + 70 + 1000 / 120);
        assert.equal(controller.model.launchStarted, true); assert.equal(controller.model.status, "");
        assert.ok(controller.model.fuel < controller.model.checkpoint.fuel);
        controller.destroy();
    }
});

test("launch-ready is quiescent and a fresh key or pointer wakes the same fixed step", async () => {
    const ready = serviceFirstSite();
    for (const authority of ["keyboard", "pointer"]) {
        const { animation, controller, timestamp } = await controllerAt(ready, performance.now());
        controller.frame(timestamp);
        const idle = { clock: structuredClone(controller.clock), pose: structuredClone(controller.model.pose),
            fuel: controller.model.fuel, renders: controller.root.setCount };
        animation.advance(timestamp, timestamp + 2000);
        assert.equal(animation.pending, 0); assert.equal(controller.frameId, null);
        assert.deepEqual(controller.clock, idle.clock); assert.deepEqual(controller.model.pose, idle.pose);
        assert.equal(controller.model.fuel, idle.fuel); assert.equal(controller.root.setCount, idle.renders);
        const edgeTime = timestamp + 2010;
        if (authority === "keyboard") controller.onKeyDown(keyEvent(controller, "keydown", "Space", edgeTime));
        else controller.onPointer(pointerEvent(controller, "pointerdown", 9, edgeTime));
        assert.equal(animation.pending, 1);
        animation.step(edgeTime + 1000 / 120);
        assert.equal(controller.model.launchStarted, true); assert.equal(controller.model.status, "");
        assert.ok(controller.model.fuel < ready.fuel); assert.ok(animation.pending > 0);
        controller.destroy();
    }
});

test("restart uses its independent scene-focus path without synthesizing launch input", async () => {
    const ready = serviceFirstSite();
    const { animation, controller } = await controllerAt({ ...ready, state: "failed" });
    controller.render(); controller.restart();
    assert.equal(controller.model.state, "launching"); assert.equal(controller.model.launchStarted, false);
    assert.equal(controller.model.status, SUCCESS_STATUS); assert.equal(globalThis.document.activeElement, controller.lander_scene_shell);
    assert.equal(controller.clock.queue.length, 0); assert.equal(controller.collectivePulse.active, false);
    assert.equal(animation.pending, 1);
    controller.destroy();
});

test("service timing exposes four vertical battery and three symmetric signal stages", () => {
    let service = serviceFirstSite(false);
    service = advanceMissionSequence(service, 0.3);
    service = advanceMissionSequence(service, 0.9);
    assert.equal(service.state, "powering");
    for (const [seconds, stage] of [[0.2,1],[0.4,2],[0.6,3],[0.8,4],[1,5],[1.2,6]]) {
        assert.equal(advanceMissionSequence(service, seconds).nocStage, stage);
    }
    const ready = advanceMissionSequence(service, 1.4);
    assert.equal(ready.nocStage, 7); assert.equal(ready.state, "launching");
    assert.equal(ready.status, SUCCESS_STATUS); assert.equal(ready.retainedSites[0].powered, true);
});

test("site structure exposes exact truss, three lattice-column, NOC, and mast envelopes", () => {
    const site = createRun({ seed: 1 }).retainedSites[0];
    const structure = siteStructure(site);
    assert.deepEqual(structure.truss, {
        bottom: site.platformBottom - 0.85, left: site.platformLeft - 0.1,
        right: structure.buildingRight + 0.1, top: site.platformBottom + 0.1,
    });
    assert.equal(structure.supportColumns.length, 3);
    assert.deepEqual(structure.supportColumns.map(({ left, right }) => [left, right]), [
        [site.platformLeft, site.platformLeft + 1],
        [site.platformLeft + 8.8, site.platformLeft + 9.8],
        [site.platformLeft + 17.6, site.platformLeft + 18.6],
    ]);
    assert.equal(structure.noc.bottom, site.platformBottom);
    assert.equal(structure.mast.right - structure.mast.left, 0.5);
    assert.ok(Math.abs(structure.mast.top - structure.mast.bottom - 3.2) < 1e-12);
});

test("independent static and dynamic site geometry stays inside exact colliders and stage pins", async () => {
    const model = createRun({ seed: 0x41475731 }); const site = model.retainedSites[0];
    const structure = siteStructure(site); const expected = expectedScaffold(site);
    assert.ok(expected.length >= 41 && expected.length <= 95);
    const template = await readFile(join(ROOT, "templates/lander-game.html"), "utf8");
    const compactTemplate = template.replace(/\s+/g, " ");
    const staticTag = template.match(/<path\s+class="site-scaffold"[^>]+>/)?.[0];
    assert.ok(staticTag); assert.match(staticTag, /stroke-width="2"/); assert.match(staticTag, /stroke-linecap="butt"/);
    assert.match(staticTag, /stroke-linejoin="round"/);
    const staticPath = staticTag.match(/d="([^"]+)"/)?.[1]; assert.ok(staticPath);
    const { LanderGameController } = await controllerClasses(); const fixture = controllerFixture();
    const controller = new LanderGameController(fixture.root); controller.model = model; controller.render();
    const group = fixture.elements["site-layer"].querySelector(`[data-site-id="${site.id}"]`);
    const dynamicSupport = group.querySelector(".site-scaffold"); const dynamicPath = dynamicSupport.attributes.get("d");
    assert.deepEqual(linearSegments(staticPath), expected); assert.deepEqual(linearSegments(dynamicPath), expected);
    assert.equal(dynamicSupport.attributes.get("stroke-width"), "2");
    assert.equal(dynamicSupport.attributes.get("stroke-linecap"), "butt");
    assert.equal(dynamicSupport.attributes.get("stroke-linejoin"), "round");
    assert.throws(() => assert.deepEqual(linearSegments(staticPath.replace(/^M312 /, "M313 ")), expected));
    withinCollider(expected.slice(0, 14), structure.truss);
    let memberIndex = 14;
    for (const column of structure.supportColumns) {
        const memberCount = 3 + 2 * column.bayCount;
        withinCollider(expected.slice(memberIndex, memberIndex + memberCount), column.collider);
        memberIndex += memberCount;
    }
    assert.equal(memberIndex, expected.length);
    for (const [width, height] of [[1,0.8],[1,0.7],[3.1,0.75]]) assert.ok(Math.hypot(width, height) < 3.2);

    const buildingLeft = site.platformRight * 10 + 20;
    const roof = Math.round((548 - structure.roof * 10) * 1e9) / 1e9;
    const battery = group.querySelector(".noc-battery"); const barTops = [46, 38, 30, 22];
    const dynamicBars = barTops.map((offset, index) => battery.querySelector(`.battery-bar-${index + 1}`).attributes.get("d"));
    const expectedBars = barTops.map((offset) => `M${buildingLeft + 29} ${roof + offset}h12v5h-12Z`);
    assert.deepEqual(dynamicBars, expectedBars);
    assert.ok(dynamicBars.every((path, index) => template.includes(`class="battery-bar battery-bar-${index + 1}" d="${path}"`)));
    assert.ok(barTops.every((value, index) => index === 0 || value < barTops[index - 1]), "bars fill bottom-to-top");
    assert.equal(battery.querySelector("rect").attributes.has("rx"), false);

    const centerX = buildingLeft + 35; const antennaY = roof - 34;
    const mast = `M${centerX} ${roof}v-32`; const head = { cx: String(centerX), cy: String(antennaY), r: "4" };
    assert.equal(group.querySelector(".antenna-mast").attributes.get("d"), mast); assert.ok(template.includes(`d="${mast}"`));
    for (const [name, value] of Object.entries(head)) assert.equal(group.querySelector(".antenna-head").attributes.get(name), value);
    assert.ok(template.includes(`class="noc-antenna antenna-head" cx="${head.cx}" cy="${head.cy}" r="4"`));
    const signalDimensions = [[8,4,12],[15,5,20],[23,6,29]];
    signalDimensions.forEach(([halfWidth, endpointRise, controlRise], index) => {
        const path = `M${centerX - halfWidth} ${antennaY - endpointRise}Q${centerX} ${antennaY - controlRise} ` +
            `${centerX + halfWidth} ${antennaY - endpointRise}`;
        assert.equal(group.querySelector(`.antenna-signal-${index + 1}`).attributes.get("d"), path);
        assert.ok(
            compactTemplate.includes(`class="noc-antenna antenna-signal antenna-signal-${index + 1}" d="${path}"`),
        );
        const [startX, startY, controlX, , endX, endY] = path.match(/-?\d+(?:\.\d+)?/g).map(Number);
        assert.equal(startX + endX, 2 * controlX); assert.equal(startY, endY);
    });
    const css = (await readFile(join(ROOT, "static/lander.css"), "utf8")).replace(/\s+/g, " ");
    assert.match(css, /\.antenna-mast, \.antenna-head \{ stroke: #292b30; \}/);
    assert.doesNotMatch(css, /data-noc-stage[^}]+\.antenna-(?:mast|head)/);
    assert.match(css, /\.antenna-signal \{ opacity: 0; \}/);
    assert.match(css, /data-noc-stage="5"[\s\S]*?\.antenna-signal-1 \{ stroke: #d94a1e; opacity: 1; \}/);
    assert.match(css, /data-noc-stage="6"[\s\S]*?\.antenna-signal-2 \{ stroke: #ff7a00; opacity: 1; \}/);
    assert.match(css, /data-noc-stage="7"\] \.antenna-signal-3 \{ stroke: #7de2c5; opacity: 1; \}/);
    controller.destroy();
});
