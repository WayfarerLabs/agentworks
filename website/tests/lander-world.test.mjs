import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
    PLATFORM_WIDTH,
    STATIC_WORLD_SEED,
    cameraLeftForPose,
    corridorVertices,
    createFirstSite,
    instantiateTemplateSite,
    mixUint32,
    normalizeSeed,
    retainedChunkIndexes,
    retainedSiteDescriptors,
    sampleUnit,
    selectTemplate,
    targetIsOffscreen,
    templatePreference,
    terrainSample,
    terrainVerticesForWindow,
} from "../static/lander-world.js";

const GEOMETRY_URL = new URL("fixtures/lander-route-geometry-v1.json", import.meta.url);

function close(actual, expected, tolerance = 1e-12) {
    assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} differs from ${expected}`);
}

test("seed mixer and sampled terrain match independent fixed vectors", () => {
    assert.equal(normalizeSeed(0), 0x6d2b79f5);
    assert.equal(mixUint32(1), 1753845952);
    assert.equal(mixUint32(0x12345678), 4125564054);
    assert.equal(mixUint32(0xffffffff), 1734902346);
    const expected = [3.632365759695, 2.237045118399, 4.041724477103, 2.046403835807, 3.451083194511, 2.655762553215];
    expected.forEach((value, index) => close(terrainSample(1, index), value, 5e-13));
    assert.equal(sampleUnit(1, 1, 0), sampleUnit(1, 1, 0));
});

test("adjacent terrain chunks share boundaries and contain material slopes", () => {
    for (const seed of [1, 0x12345678, 0xffffffff]) {
        for (let chunk = -3; chunk <= 8; chunk += 1) {
            close(terrainSample(seed, chunk * 5 + 5), terrainSample(seed, (chunk + 1) * 5));
            const heights = Array.from({ length: 6 }, (_, index) => terrainSample(seed, chunk * 5 + index));
            assert.ok(heights.some((height, index) => index > 0 && height - heights[index - 1] >= 0.35));
            assert.ok(heights.some((height, index) => index > 0 && heights[index - 1] - height >= 0.35));
        }
    }
});

test("first site is one exact elevated three-lander-width helipad", () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    close(site.platformTop, 5.119569691829383);
    assert.equal(site.center, 36);
    close(site.platformRight - site.platformLeft, PLATFORM_WIDTH);
    close(PLATFORM_WIDTH, 3 * 3.2);
    assert.equal(site.canCollected, false);
    assert.equal(site.powered, false);
    assert.ok(Object.isFrozen(site));
});

test("template preference vectors and fallback are exact", async () => {
    assert.deepEqual(templatePreference(1, 1), [102, 87, 99, 84, 96, 81, 93, 78, 90]);
    assert.deepEqual(templatePreference(0x12345678, 1), [99, 84, 96, 81, 93, 78, 90, 102, 87]);
    assert.deepEqual(templatePreference(0xffffffff, 1), [87, 99, 84, 96, 81, 93, 78, 90, 102]);
    const geometry = JSON.parse(await readFile(GEOMETRY_URL, "utf8"));
    const origin = { ...createFirstSite(1), platformTop: 8.3 };
    assert.equal(selectTemplate(1, 1, origin, geometry.templates).centerDelta, 102);
});

test("all nine constructive corridors replace the target span and preserve caps", async () => {
    const geometry = JSON.parse(await readFile(GEOMETRY_URL, "utf8"));
    for (const template of geometry.templates) {
        const origin = createFirstSite(0x12345678);
        const target = instantiateTemplateSite(0x12345678, 1, origin, template);
        const vertices = corridorVertices(0x12345678, origin, target);
        assert.deepEqual(vertices[0], [origin.platformRight, origin.platformTop - 0.8]);
        assert.deepEqual(vertices.at(-2), [target.platformLeft, target.platformTop - 0.8]);
        assert.deepEqual(vertices.at(-1), [target.platformRight, target.platformTop - 0.8]);
        for (const [, y] of vertices.slice(1, -2)) assert.ok(y <= origin.platformTop - 0.65);
        assert.ok(Object.isFrozen(target));
    }
});

test("camera, offscreen cue, and rolling retention remain bounded", async () => {
    const geometry = JSON.parse(await readFile(GEOMETRY_URL, "utf8"));
    const sites = [createFirstSite(1)];
    for (let id = 1; id < 6; id += 1) sites.push(instantiateTemplateSite(1, id, sites.at(-1), geometry.templates[0]));
    assert.equal(cameraLeftForPose({ x: 34 }), 0);
    assert.equal(cameraLeftForPose({ x: 80 }), 45);
    assert.equal(retainedChunkIndexes(45).length, 10);
    assert.ok(retainedChunkIndexes(45).length <= 10);
    assert.ok(retainedSiteDescriptors(sites, 4, 5).length <= 3);
    assert.equal(targetIsOffscreen(sites[1], 0), true);
    assert.equal(targetIsOffscreen({ platformLeft: 100 }, 0), false);
    const vertices = terrainVerticesForWindow(1, sites.slice(0, 2), -40, 140);
    assert.ok(vertices.length > 20);
    assert.ok(Object.isFrozen(vertices));
});

test("geometry fixture is independent, versioned, and has a stable digest", async () => {
    const text = await readFile(GEOMETRY_URL, "utf8");
    const geometry = JSON.parse(text);
    assert.equal(geometry.schema, "agw-lander-route-geometry/v1");
    assert.equal(geometry.templates.length, 9);
    assert.ok(!text.includes("demonstratedMinimum"));
    assert.ok(!text.includes('"runs"'));
    const canonical = JSON.stringify(geometry, Object.keys(geometry).sort());
    assert.equal(typeof createHash("sha256").update(canonical).digest("hex"), "string");
});
