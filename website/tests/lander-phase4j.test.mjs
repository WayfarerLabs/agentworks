import assert from "node:assert/strict";
import test from "node:test";

import {
    advanceMissionSequence,
    agentInstalled,
    createRun,
    fuelGaugeLevel,
    stepFlight,
    transitionMission,
} from "../static/lander-model.js";
import { cameraLeftForPose } from "../static/lander-world.js";
import { FakeElement, controllerClasses, controllerFixture, descendantCount } from "./lander-test-dom.mjs";

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

test("normal refuel commits fuel atomically and owns one exact 300 ms linear projection", () => {
    const service = beginService(false, 7.5);
    assert.equal(service.state, "landed");
    assert.ok(service.fuel > 7.5, "the real award is committed at contact");
    assert.equal(service.fuelGaugeReference, service.fuel);
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
        assert.equal(result.fuel, result.fuelGaugeReference);
        assert.equal(fuelGaugeLevel(result), 1);
        assert.ok(result.checkpoint);
    }
    assert.equal(toggled.fuel, reduced.fuel);
    assert.deepEqual(toggled.checkpoint, reduced.checkpoint);
});

test("refuel presentation is excluded from failure, restart, and preflight authority", () => {
    let ready = advanceMissionSequence(beginService(false), 0.3);
    ready = advanceMissionSequence(ready, 0.9);
    ready = advanceMissionSequence(ready, 1.4);
    assert.equal(ready.refuel, null);
    const failed = { ...ready, state: "failed", refuel: { siteId: 0, fromLevel: 0.2, progress: 0.4 } };
    const restored = transitionMission(failed, "RESTART");
    assert.equal(restored.refuel, null);
    assert.equal(transitionMission(restored, "EXIT").refuel, null);
});

test("installed-agent projection begins at NOC stage one and survives powered checkpoints", () => {
    assert.equal(agentInstalled({ powered: false, nocStage: 0 }), false);
    assert.equal(agentInstalled({ powered: false, nocStage: 1 }), true);
    assert.equal(agentInstalled({ powered: true, nocStage: 0 }), true);
    let model = advanceMissionSequence(beginService(false), 0.3);
    model = advanceMissionSequence(model, 0.9);
    const stageZero = model.retainedSites.find((site) => site.id === model.activeSiteId);
    assert.equal(agentInstalled(stageZero), false);
    const stageOneModel = advanceMissionSequence(model, 0.2);
    assert.equal(agentInstalled(stageOneModel.retainedSites.find((site) => site.id === model.activeSiteId)), true);
    const powered = advanceMissionSequence(model, 1.4);
    const restarted = transitionMission({ ...powered, state: "failed" }, "RESTART");
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

test("fuel height and exact empty, danger, caution, and ready colors are independent projections", async () => {
    const run = createRun({ seed: 1 });
    const { controller, root } = await controllerAt(run);
    for (const [level, name, color] of [
        [0, "empty", "#ff5a36"],
        [0.2, "danger", "#ff5a36"],
        [0.200001, "caution", "#ffb000"],
        [0.5, "caution", "#ffb000"],
        [0.500001, "ready", "#2ed49b"],
    ]) {
        controller.model = { ...run, fuel: run.fuelGaugeReference * level };
        controller.render();
        assert.equal(root.style.getPropertyValue("--fuel-gauge-level"), String(level));
        assert.equal(root.dataset.fuelLevel, name);
        assert.equal(root.style.getPropertyValue("--fuel-level-color"), color);
    }
    controller.model = {
        ...run,
        fuel: 8,
        refuel: { siteId: 1, fromLevel: 0, progress: 0 },
    };
    controller.render();
    assert.equal(root.style.getPropertyValue("--fuel-gauge-level"), "0");
    assert.equal(root.dataset.fuelLevel, "danger");
    controller.destroy();
});

test("action descendants never create pointer flight input and stage owns all pointer listeners", async () => {
    const run = createRun({ seed: 1 });
    const { controller, elements } = await controllerAt(run);
    assert.equal((elements["lander-scene-shell"].listeners.get("pointerdown") ?? []).length, 0);
    assert.equal((elements["lander-scene-stage"].listeners.get("pointerdown") ?? []).length, 1);
    for (const id of ["lander-restart"]) {
        const descendant = new FakeElement(elements[id]);
        let prevented = false;
        const before = { token: controller.pointerToken, queue: controller.clock.queue.length };
        controller.onPointer({ type: "pointerdown", pointerId: 8, isPrimary: true, button: 0,
            clientX: 200, clientY: 100, timeStamp: 10, composedPath: () =>
                [descendant, elements[id], elements["lander-outcome"], elements["lander-scene-stage"]],
            preventDefault() { prevented = true; } });
        assert.equal(prevented, false, id);
        assert.equal(controller.pointer, null, id);
        assert.equal(controller.pointerToken, before.token, id);
        assert.equal(controller.clock.queue.length, before.queue, id);
        assert.equal(elements["lander-scene-stage"].hasPointerCapture(8), false, id);
    }
    controller.destroy();
});

test("active description follows the exact offscreen predicate without referencing hidden text", async () => {
    const run = createRun({ seed: 1 });
    const target = run.retainedSites.find((site) => site.id === run.targetSiteId);
    const boundary = cameraLeftForPose(run.pose) + 100;
    const withTargetLeft = (platformLeft) => ({
        ...run,
        retainedSites: run.retainedSites.map((site) => site.id === target.id ? { ...site, platformLeft } : site),
    });
    const { controller, elements } = await controllerAt(withTargetLeft(boundary));
    const permanent = ["lander-scene-description", "lander-controls", "lander-fuel-label",
        "lander-fuel-value", "lander-status"];
    assert.deepEqual(elements["lander-scene-shell"].attributes.get("aria-describedby").split(" "), permanent);
    assert.equal(elements["lander-target-direction"].hidden, true);

    controller.model = withTargetLeft(boundary + Number.EPSILON * boundary);
    controller.render();
    assert.deepEqual(elements["lander-scene-shell"].attributes.get("aria-describedby").split(" "),
        [...permanent.slice(0, -1), "lander-target-direction", permanent.at(-1)]);
    assert.equal(elements["lander-target-direction"].hidden, false);
    controller.destroy();
});

test("outcome projection keeps structural banner/action states and source order", async () => {
    const ready = { ...advanceMissionSequence(beginService(true), 0), status: "sentinel" };
    const { controller, elements, root } = await controllerAt(ready);
    assert.deepEqual(elements["lander-outcome"].children,
        [elements["lander-status"], elements["lander-restart"]]);
    assert.deepEqual(elements["lander-controls-rail"].children,
        [elements["lander-controls"], elements["lander-exit"]]);
    assert.equal(root.dataset.banner, "deployed");
    assert.equal(elements["lander-restart"].hidden, true);
    assert.equal(elements["lander-exit"].hidden, false);
    controller.model = { ...ready, state: "failed" };
    controller.render();
    assert.equal(root.dataset.banner, "crashed");
    assert.equal(elements["lander-restart"].hidden, false);
    assert.equal(elements["lander-status"].textContent, controller.model.status);
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
