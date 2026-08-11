import assert from "node:assert/strict";
import test from "node:test";

import {
    FAILURE_STATUS,
    ROUTE_DIGESTS,
    SUCCESS_STATUS,
    advanceMissionSequence,
    agentInstalled,
    createRun,
    fuelGaugeLevel,
    stepFlight,
    transitionMission,
} from "../static/lander-model.js";
import { cameraLeftForPose } from "../static/lander-world.js";
import { FakeElement, controllerClasses, controllerFixture, descendantCount } from "./lander-test-dom.mjs";

const EXPECTED_DIGESTS = Object.freeze({
    geometryDigest: "e91ce3a27c011ef6b2549fdc36fa6e25db5c5da2d274233c9da4fc8adf4a0244",
    outputDigest: "0e1261c0d8ab22bb98c2c736714598bfc08cc4c8cd43f32615d39b14c46977bd",
    physicsDigest: "0a57c2543fb3010e468c0550aeceac206727aa97f47e52157e2350992ea0f8d9",
    worldDigest: "535f190fdf7c7300a7667ce2a3e6d5f1395b197b0bd27c2dbb0f69f61310333a",
});
const INSTALLED_PATH = "M -4 -9 H 4 A 1 1 0 0 1 5 -8 V 1 A 1 1 0 0 1 4 2 " +
    "H -4 A 1 1 0 0 1 -5 1 V -8 A 1 1 0 0 1 -4 -9 Z " +
    "M -3 2 V 9 M 3 2 V 9 M -2 -5 L 0 -3 L -2 -1 M 1 -1 H 3";

function firstSiteApproach(reducedMotion = false, fuel = 7.5) {
    const model = createRun({ seed: 1, reducedMotion });
    const target = model.retainedSites[0];
    return { ...model, fuel, pose: { x: target.center, y: target.platformTop + 0.001,
        vx: 0, vy: -1, angle: 0, angularVelocity: 0 } };
}

function beginService(reducedMotion = false, fuel = 7.5) {
    return stepFlight(firstSiteApproach(reducedMotion, fuel), { left: 0, right: 0 });
}

async function controllerAt(model) {
    const { LanderGameController } = await controllerClasses();
    globalThis.document.hidden = true;
    const fixture = controllerFixture();
    const controller = new LanderGameController(fixture.root);
    controller.model = model;
    controller.render();
    return { controller, ...fixture };
}

test("Phase 4J preserves every Phase 4I route, physics, geometry, world, and output digest", () => {
    assert.deepEqual(ROUTE_DIGESTS, EXPECTED_DIGESTS);
});

test("normal refuel commits fuel atomically and owns one exact 300 ms linear projection", () => {
    const service = beginService(false, 7.5);
    assert.equal(service.state, "landed");
    assert.ok(service.fuel > 7.5, "the real award is committed at contact");
    assert.equal(service.legDepartureFuel, service.fuel);
    assert.deepEqual(service.refuel, { siteId: 0, fromLevel: 0.25, progress: 0 });
    for (const [seconds, progress, level] of [[0,0,0.25],[0.15,0.5,0.625],[0.299,0.9966666666666667,0.9975]]) {
        const sample = advanceMissionSequence(service, seconds);
        assert.equal(sample.refuel.progress, progress);
        assert.ok(Math.abs(fuelGaugeLevel(sample) - level) < 1e-12);
        assert.equal(sample.fuel, service.fuel);
        assert.equal(sample.checkpoint, null);
    }
    const complete = advanceMissionSequence(service, 0.3);
    assert.equal(complete.state, "deploying");
    assert.equal(complete.refuel, null);
    assert.equal(fuelGaugeLevel(complete), 1);
});

test("reduced motion and a mid-refuel motion change reach the same atomic launch-ready result", () => {
    const normal = beginService(false, 7.5);
    const toggled = advanceMissionSequence(advanceMissionSequence(normal, 0.15), 3.1, true);
    const reduced = beginService(true, 7.5);
    assert.equal(reduced.state, "launching");
    assert.equal(reduced.refuel, null);
    assert.equal(toggled.state, "launching");
    assert.equal(toggled.refuel, null);
    for (const result of [reduced, toggled]) {
        assert.equal(result.status, SUCCESS_STATUS);
        assert.equal(result.fuel, result.legDepartureFuel);
        assert.equal(fuelGaugeLevel(result), 1);
        assert.ok(result.checkpoint);
    }
    assert.equal(toggled.fuel, reduced.fuel);
    assert.deepEqual(toggled.checkpoint, reduced.checkpoint);
});

test("refuel presentation is excluded from failure, restart, and preflight authority", () => {
    let ready = advanceMissionSequence(beginService(false), 0.3);
    ready = advanceMissionSequence(ready, 1.8);
    ready = advanceMissionSequence(ready, 1.4);
    assert.equal(ready.refuel, null);
    const failed = { ...ready, state: "failed", refuel: { siteId: 0, fromLevel: 0.2, progress: 0.4 },
        status: FAILURE_STATUS };
    const restored = transitionMission(failed, "RESTART");
    assert.equal(restored.refuel, null);
    assert.equal(restored.status, SUCCESS_STATUS);
    assert.equal(transitionMission(restored, "EXIT").refuel, null);
    assert.equal(FAILURE_STATUS, "Crashed!");
});

test("installed-agent projection begins at NOC stage one and survives powered checkpoints", () => {
    assert.equal(agentInstalled({ powered: false, nocStage: 0 }), false);
    assert.equal(agentInstalled({ powered: false, nocStage: 1 }), true);
    assert.equal(agentInstalled({ powered: true, nocStage: 0 }), true);
    let model = advanceMissionSequence(beginService(false), 0.3);
    model = advanceMissionSequence(model, 1.8);
    const stageZero = model.retainedSites.find((site) => site.id === model.activeSiteId);
    assert.equal(agentInstalled(stageZero), false);
    const stageOneModel = advanceMissionSequence(model, 0.2);
    assert.equal(agentInstalled(stageOneModel.retainedSites.find((site) => site.id === model.activeSiteId)), true);
    const powered = advanceMissionSequence(model, 1.4);
    const restarted = transitionMission({ ...powered, state: "failed", status: FAILURE_STATUS }, "RESTART");
    assert.ok(restarted.retainedSites.filter((site) => site.powered).every(agentInstalled));
});

test("the existing NOC entry switches exact geometry and restores absent without adding a node", async () => {
    const run = createRun({ seed: 1 });
    const site = run.retainedSites[0];
    const { controller, elements } = await controllerAt(run);
    const group = elements["site-layer"].querySelector(`[data-site-id="${site.id}"]`);
    const entry = group.querySelector(".noc-entry");
    const count = descendantCount(group);
    assert.equal(group.dataset.agent, "absent");
    assert.equal(entry.attributes.has("transform"), false);
    controller.model = { ...run, retainedSites: run.retainedSites.map((item) =>
        item.id === site.id ? { ...item, nocStage: 1 } : item) };
    controller.render();
    assert.equal(group.dataset.agent, "installed");
    assert.equal(entry.attributes.get("d"), INSTALLED_PATH);
    assert.match(entry.attributes.get("transform"), /^translate\(.+\) scale\(0\.75\)$/);
    assert.equal(descendantCount(group), count);
    controller.model = run;
    controller.render();
    assert.equal(group.dataset.agent, "absent");
    assert.equal(entry.attributes.has("transform"), false);
    assert.match(entry.attributes.get("d"), /^M .+ H .+ V .+ H .+ Z$/);
    assert.equal(descendantCount(group), count);
    controller.destroy();
});

test("refuel transfer uses the exact stage-local linear frame and resize reprojects without time", async () => {
    const run = createRun({ seed: 1 });
    const site = { ...run.retainedSites[0], id: 20, center: 10, platformTop: 10 };
    const model = { ...run, retainedSites: [site], activeSiteId: 20,
        refuel: { siteId: 20, fromLevel: 0.25, progress: 0.25 } };
    const { controller, elements, root } = await controllerAt(model);
    elements["lander-scene-stage"].rect = { left: 100, top: 50, width: 1000, height: 640 };
    elements["lander-fuel-gauge"].rect = { left: 120, top: 70, width: 16, height: 112 };
    controller.renderRefuel(0);
    assert.equal(root.dataset.refueling, "true");
    assert.equal(root.style.getPropertyValue("--refuel-progress"), "0.25");
    assert.equal(root.style.getPropertyValue("--fuel-transfer-x"), "104.5px");
    assert.equal(root.style.getPropertyValue("--fuel-transfer-y"), "343.75px");
    elements["lander-scene-stage"].rect = { left: 40, top: 20, width: 500, height: 320 };
    elements["lander-fuel-gauge"].rect = { left: 50, top: 30, width: 16, height: 112 };
    controller.renderRefuel(0);
    assert.equal(controller.model.refuel.progress, 0.25);
    assert.notEqual(root.style.getPropertyValue("--fuel-transfer-x"), "104.5px");
    controller.destroy();
});

test("fuel height and exact danger, caution, and ready colors are independent projections", async () => {
    const run = createRun({ seed: 1 });
    const { controller, root } = await controllerAt(run);
    for (const [level, name, color] of [
        [0.2, "danger", "#ff5a36"],
        [0.200001, "caution", "#ffb000"],
        [0.5, "caution", "#ffb000"],
        [0.500001, "ready", "#2ed49b"],
    ]) {
        controller.model = { ...run, fuel: run.legDepartureFuel * level };
        controller.render();
        assert.equal(root.style.getPropertyValue("--fuel-gauge-level"), String(level));
        assert.equal(root.dataset.fuelLevel, name);
        assert.equal(root.style.getPropertyValue("--fuel-level-color"), color);
    }
    controller.destroy();
});

test("action descendants never create pointer flight input and stage owns all pointer listeners", async () => {
    const run = createRun({ seed: 1 });
    const { controller, elements } = await controllerAt(run);
    assert.equal((elements["lander-scene-shell"].listeners.get("pointerdown") ?? []).length, 0);
    assert.equal((elements["lander-scene-stage"].listeners.get("pointerdown") ?? []).length, 1);
    for (const id of ["lander-launch", "lander-restart", "lander-exit"]) {
        const descendant = new FakeElement(elements[id]);
        let prevented = false;
        const before = { token: controller.pointerToken, queue: controller.clock.queue.length };
        controller.onPointer({ type: "pointerdown", pointerId: 8, isPrimary: true, button: 0,
            clientX: 200, clientY: 100, timeStamp: 10, composedPath: () =>
                [descendant, elements[id], elements["lander-actions"], elements["lander-scene-stage"]],
            preventDefault() { prevented = true; } });
        assert.equal(prevented, false, id);
        assert.equal(controller.pointer, null, id);
        assert.equal(controller.pointerToken, before.token, id);
        assert.equal(controller.clock.queue.length, before.queue, id);
        assert.equal(elements["lander-scene-stage"].hasPointerCapture(8), false, id);
    }
    controller.destroy();
});

test("outcome projection keeps exact banner/action states and source order", async () => {
    const ready = advanceMissionSequence(beginService(true), 0);
    const { controller, elements, root } = await controllerAt(ready);
    assert.deepEqual(elements["lander-actions"].children,
        [elements["lander-launch"], elements["lander-restart"], elements["lander-exit"]]);
    assert.equal(root.dataset.banner, "deployed");
    assert.equal(elements["lander-launch"].hidden, false);
    assert.equal(elements["lander-restart"].hidden, true);
    assert.equal(elements["lander-exit"].hidden, false);
    controller.model = { ...ready, state: "failed", status: FAILURE_STATUS };
    controller.render();
    assert.equal(root.dataset.banner, "crashed");
    assert.equal(elements["lander-launch"].hidden, true);
    assert.equal(elements["lander-restart"].hidden, false);
    assert.equal(elements["lander-status"].textContent, "Crashed!");
    controller.model = { ...ready, state: "generation-error",
        status: "Mission generation failed. Use Exit mission to start a new run." };
    controller.render();
    assert.equal(root.dataset.banner, "error");
    assert.equal(elements["lander-launch"].hidden, true);
    assert.equal(elements["lander-restart"].hidden, true);
    controller.destroy();
});

test("hidden lifecycle freezes refuel progress and visible reprojection keeps the same model time", async () => {
    const model = advanceMissionSequence(beginService(false), 0.15);
    const { controller } = await controllerAt(model);
    const progress = controller.model.refuel.progress;
    globalThis.document.hidden = true;
    controller.onVisibilityChange();
    assert.equal(controller.model.refuel.progress, progress);
    assert.equal(controller.root.dataset.paused, "true");
    globalThis.document.hidden = false;
    controller.onVisibilityChange();
    assert.equal(controller.model.refuel.progress, progress);
    assert.equal(cameraLeftForPose(controller.model.pose), cameraLeftForPose(model.pose));
    controller.destroy();
});
