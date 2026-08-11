import assert from "node:assert/strict";
import test from "node:test";

import {
    SUCCESS_STATUS,
    TURN_DIFFERENTIAL,
    advanceMissionSequence,
    createRun,
    fuelGaugeLevel,
    stepFlight,
    transitionMission,
} from "../static/lander-model.js";
import { siteStructure } from "../static/lander-world.js";

function serviceFirstSite(reducedMotion = true) {
    let model = createRun({ seed: 1, reducedMotion });
    const target = model.retainedSites[0];
    model = { ...model, pose: { x: target.center, y: target.platformTop + 0.001,
        vx: 0, vy: -1, angle: 0, angularVelocity: 0 } };
    return stepFlight(model, { left: 0, right: 0 });
}

test("leg-relative gauge is exact, unbounded, and restored with the checkpoint", () => {
    const run = createRun({ seed: 1 });
    assert.equal(run.fuel, 30); assert.equal(run.legDepartureFuel, 30); assert.equal(fuelGaugeLevel(run), 1);
    assert.equal(fuelGaugeLevel({ ...run, fuel: 37.5, legDepartureFuel: 50 }), 0.75);
    assert.equal(fuelGaugeLevel({ ...run, fuel: 75, legDepartureFuel: 50 }), 1);
    assert.equal(fuelGaugeLevel({ ...run, fuel: 5, legDepartureFuel: 0 }), 0);
    const ready = serviceFirstSite();
    assert.equal(ready.fuel, ready.legDepartureFuel); assert.equal(fuelGaugeLevel(ready), 1);
    const crashed = { ...ready, state: "failed", fuel: ready.fuel - 5 };
    const restored = transitionMission(crashed, "RESTART");
    assert.equal(restored.fuel, ready.checkpoint.fuel);
    assert.equal(restored.legDepartureFuel, ready.checkpoint.legDepartureFuel);
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

test("service timing exposes four vertical battery and three symmetric signal stages", () => {
    let service = serviceFirstSite(false);
    service = advanceMissionSequence(service, 0.3);
    service = advanceMissionSequence(service, 1.8);
    assert.equal(service.state, "powering");
    for (const [seconds, stage] of [[0.2,1],[0.4,2],[0.6,3],[0.8,4],[1,5],[1.2,6]]) {
        assert.equal(advanceMissionSequence(service, seconds).nocStage, stage);
    }
    const ready = advanceMissionSequence(service, 1.4);
    assert.equal(ready.nocStage, 7); assert.equal(ready.state, "launching");
    assert.equal(ready.status, SUCCESS_STATUS); assert.equal(ready.retainedSites[0].powered, true);
});

test("site structure exposes exact conservative scaffold, connector, NOC, and mast envelopes", () => {
    const site = createRun({ seed: 1 }).retainedSites[0];
    const structure = siteStructure(site);
    assert.deepEqual(structure.platformUnderframe, {
        bottom: site.platformTop - 2.5, left: site.platformLeft - 0.1,
        right: site.platformRight + 0.1, top: site.platformTop - 0.25,
    });
    assert.deepEqual(structure.connector, {
        bottom: site.platformTop - 0.35 - 0.1, left: site.platformRight - 0.1,
        right: site.platformRight + 2.1, top: site.platformTop + 0.1,
    });
    assert.equal(structure.nocUnderframe.left, site.platformRight + 1.9);
    assert.equal(structure.nocUnderframe.right, site.platformRight + 9.1);
    assert.equal(structure.mast.right - structure.mast.left, 0.5);
    assert.ok(Math.abs(structure.mast.top - structure.mast.bottom - 3.2) < 1e-12);
});
