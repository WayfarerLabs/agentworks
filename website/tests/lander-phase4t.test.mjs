import assert from "node:assert/strict";
import test from "node:test";

import { createRun, stepFlight, transitionMission, updateRetention } from "../static/lander-model.js";
import {
    MAX_NORMALIZED_DECK,
    SITE_CANDIDATE_OFFSETS,
    SITE_CANDIDATE_ORDERS,
    STATIC_WORLD_SEED,
    TERRAIN_PROFILES,
    WORLD_MAX_X,
    createSiteForIndex,
    terrainProfileForBlock,
} from "../static/lander-world.js";

const IDS = Object.freeze(Array.from({ length: 8 }, (_, index) => `S${index}`));

function reversalCount(samples) {
    const grades = samples.slice(1).map((sample, index) => Math.round((sample - samples[index]) * 400) / 100);
    return grades.filter((grade, index) => Math.sign(grade) !== Math.sign(grades[(index + 1) % grades.length])).length;
}

function contactTarget(model) {
    const target = model.retainedSites.find((site) => site.id === model.targetSiteId);
    return updateRetention(
        stepFlight(
            {
                ...model,
                pose: { x: target.center, y: target.platformTop + 0.001, vx: 0, vy: -1, angle: 0, angularVelocity: 0 },
            },
            { left: 0, right: 0 },
        ),
    );
}

test("Phase 4T reversal density and bounded candidate order remain preserved", () => {
    assert.deepEqual(IDS.map((id) => reversalCount(TERRAIN_PROFILES[id])), [12, 12, 16, 16, 16, 12, 16, 12]);
    assert.deepEqual(SITE_CANDIDATE_OFFSETS, [0, 8, 16, 24, 32, 40]);
    assert.deepEqual(SITE_CANDIDATE_ORDERS, [[0, 1, 2, 3, 4, 5], [0, 5, 4, 3, 2, 1]]);
    assert.equal(MAX_NORMALIZED_DECK, 0.5);
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        const selected = Array.from({ length: 128 }, (_, offset) => terrainProfileForBlock(seed, offset - 64).profile);
        assert.equal(new Set(selected).size, 8);
        assert.ok(selected.every((profile, index) => index === 0 || profile !== selected[index - 1]));
        const order = createSiteForIndex(seed, -100).candidateOrder;
        for (let index = -100; index <= 100; index += 1) {
            const site = createSiteForIndex(seed, index);
            assert.equal(site.candidateOrder, order);
            assert.ok(site.candidateOrdinal <= 5);
            assert.ok(site.normalizedDeck <= MAX_NORMALIZED_DECK);
        }
    }
});

test("Phase 4T checkpoint identity survives direct-allowance migration", () => {
    let model = contactTarget(createRun({ seed: STATIC_WORLD_SEED, reducedMotion: true }));
    assert.equal(model.state, "launching");
    assert.equal("targetRouteProof" in model, false);
    const checkpoint = structuredClone(model.checkpoint);
    model = transitionMission({ ...model, state: "failed" }, "RESTART");
    assert.equal(model.state, "launching");
    assert.deepEqual(model.retainedSites, checkpoint.retainedSites);
    assert.equal(model.targetSiteId, checkpoint.targetSiteId);
    assert.equal("targetRouteProof" in model, false);
});

test("Phase 4T finite final-site placement remains inside the expanded physical rail", () => {
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        const final = createSiteForIndex(seed, 4095);
        assert.ok(final.center + 13.8 < WORLD_MAX_X);
    }
});
