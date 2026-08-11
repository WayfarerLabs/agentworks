import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
    CHUNK_WIDTH,
    MOTIFS,
    PLATFORM_WIDTH,
    STATIC_WORLD_SEED,
    cameraLeftForPose,
    corridorVertices,
    createFirstSite,
    instantiateTemplateSite,
    mixUint32,
    motifIndex,
    motifSelection,
    normalizeSeed,
    retainedChunkIndexes,
    retainedSiteDescriptors,
    sampleUnit,
    selectTemplate,
    siteFoundationBottom,
    targetIsOffscreen,
    templatePreference,
    terrainHeightFromVertices,
    terrainPath,
    terrainSample,
    terrainVerticesForRange,
    terrainVerticesForWindow,
} from "../static/lander-world.js";

const GEOMETRY_URL = new URL("fixtures/lander-route-geometry-v2.json", import.meta.url);
const TEMPLATE_URL = new URL("../templates/lander-game.html", import.meta.url);

function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") {
        return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
    }
    return value;
}

function close(actual, expected, tolerance = 1e-12) {
    assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} differs from ${expected}`);
}

test("seed mixer and sampled terrain match independent fixed vectors", () => {
    assert.equal(normalizeSeed(0), 0x6d2b79f5);
    assert.equal(mixUint32(1), 1753845952);
    assert.equal(mixUint32(0x12345678), 4125564054);
    assert.equal(mixUint32(0xffffffff), 1734902346);
    const vectors = [
        { seed: 1, selection: { direction: 1, offset: 0 }, motifs: [0,1,2,3],
            heights: [3.948548639542423,6.055567677598447,1.8625867156544702,
                4.869605753710493,1.6766247917665167,2.4836438298225403], top: 6.886448038277216 },
        { seed: 0x12345678, selection: { direction: 3, offset: 2 }, motifs: [2,1,0,3],
            heights: [2.8413594241719693,3.9342738820938394,5.72718834001571,
                4.02010279793758,1.7130172558594494,3.8059317137813196], top: 6.164073424622881 },
        { seed: 0xffffffff, selection: { direction: 1, offset: 1 }, motifs: [1,2,3,0],
            heights: [2.9631244149059057,0.763576190569438,1.9640279662329705,
                4.864479741896503,3.564931517560035,2.4653832932235673], top: 7.1085339549761265 },
    ];
    for (const vector of vectors) {
        vector.heights.forEach((value, index) => close(terrainSample(vector.seed, index), value));
        assert.deepEqual(motifSelection(vector.seed), vector.selection);
        assert.deepEqual(Array.from({ length: 4 }, (_, index) => motifIndex(vector.seed, index)), vector.motifs);
        close(createFirstSite(vector.seed).platformTop, vector.top);
    }
    assert.deepEqual(MOTIFS, [[0,2.4,-1.5,1.8,-1.1,0],[0,-2.1,-0.8,2.2,1,0],
        [0,0.9,2.5,0.6,-1.9,0],[0,-1.4,1.3,2.4,-0.5,0]]);
    close(sampleUnit(1, 1, 0), 0.5441219198983163);
    close(sampleUnit(1, 4, 7), 0.29075191100127995);
    close(sampleUnit(0x12345678, 3, 99), 0.38062425260432065);
    close(sampleUnit(0xffffffff, 5, 0), 0.4930636757053435);
});

test("adjacent terrain chunks share boundaries and contain material slopes", () => {
    for (const seed of [1, 0x12345678, 0xffffffff]) {
        for (let chunk = -3; chunk <= 8; chunk += 1) {
            close(terrainSample(seed, chunk * 5 + 5), terrainSample(seed, (chunk + 1) * 5));
            const heights = Array.from({ length: 6 }, (_, index) => terrainSample(seed, chunk * 5 + index));
            assert.ok(heights.some((height, index) => index > 0 && height - heights[index - 1] >= 0.35));
            assert.ok(heights.some((height, index) => index > 0 && heights[index - 1] - height >= 0.35));
        }
        assert.equal(new Set(Array.from({ length: 4 }, (_, index) => motifIndex(seed, index - 2))).size, 4);
    }
});

test("rendered chunk ranges clip crossing shelves to exact collision boundaries", () => {
    const collisionTerrain = Object.freeze([
        Object.freeze([0, 2]), Object.freeze([40, 4]), Object.freeze([64, 4]),
        Object.freeze([100, 3]), Object.freeze([140, 6]), Object.freeze([162, 6]),
        Object.freeze([200, 2]),
    ]);
    for (const boundary of [50, 150]) {
        const left = terrainVerticesForRange(collisionTerrain, boundary - 50, boundary);
        const right = terrainVerticesForRange(collisionTerrain, boundary, boundary + 50);
        assert.deepEqual(left.at(-1), right[0]);
        assert.equal(left.at(-1)[0], boundary);
        assert.equal(left.at(-1)[1], terrainHeightFromVertices(collisionTerrain, boundary));
        assert.ok(terrainPath(left).includes(`L${boundary * 10} `));
        assert.ok(terrainPath(right).startsWith(`M${boundary * 10} `));
    }
});

test("first site is one exact elevated three-lander-width helipad", () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    close(site.platformTop, 9.584423104863614);
    assert.equal(site.center, 36);
    close(site.platformRight - site.platformLeft, PLATFORM_WIDTH);
    close(site.shelfRight, site.platformRight + 9);
    close(PLATFORM_WIDTH, 3 * 3.2);
    assert.equal(site.canCollected, false);
    assert.equal(site.powered, false);
    assert.ok(Object.isFrozen(site));
});

test("static world exactly renders the retained collision terrain and NOC foundation", async () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    const vertices = terrainVerticesForWindow(STATIC_WORLD_SEED, [site], -40, 140);
    const expectedSceneY = 548 - site.platformTop * 10;
    const template = await readFile(TEMPLATE_URL, "utf8");
    const rendered = [...template.matchAll(
        /<path\s+class="terrain-chunk"\s+data-chunk-index="(-?\d+)"\s+d="([^"]+)"/g,
    )].map((match) => ({ index: Number(match[1]), path: match[2] }));
    assert.deepEqual(rendered, retainedChunkIndexes(0).map((index) => {
        const left = index * CHUNK_WIDTH;
        const chunk = terrainVerticesForRange(vertices, left, left + CHUNK_WIDTH);
        return { index, path: terrainPath(chunk) };
    }));
    assert.equal(rendered[0].path.startsWith("M-400 "), true);
    assert.equal(rendered.at(-1).path.includes("L1400 648L1000 648Z"), true);
    const match = template.match(/<path class="noc-building" d="M428 ([0-9.]+)V/);
    assert.ok(match);
    close(Number(match[1]), expectedSceneY, 0.001);
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
        assert.deepEqual(vertices[0], [origin.shelfRight, origin.platformTop - 2.4]);
        assert.deepEqual(vertices.at(-2), [target.platformLeft, target.platformTop - 2.4]);
        assert.deepEqual(vertices.at(-1), [target.shelfRight, target.platformTop - 2.4]);
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
    assert.equal(CHUNK_WIDTH, 50);
    assert.equal(retainedChunkIndexes(25).length, 5);
    assert.ok(retainedChunkIndexes(45).length <= 5);
    assert.ok(retainedSiteDescriptors(sites, 4, 5).length <= 3);
    assert.equal(targetIsOffscreen(sites[1], 0), true);
    assert.equal(targetIsOffscreen({ platformLeft: 100 }, 0), false);
    const vertices = terrainVerticesForWindow(1, sites.slice(0, 2), -40, 140);
    assert.ok(vertices.length > 10);
    assert.ok(Object.isFrozen(vertices));
});

test("geometry fixture is independent, versioned, and has a stable digest", async () => {
    const text = await readFile(GEOMETRY_URL, "utf8");
    const geometry = JSON.parse(text);
    assert.equal(geometry.schema, "agw-lander-route-geometry/v2");
    assert.equal(geometry.templates.length, 9);
    assert.ok(!text.includes("demonstratedMinimum"));
    assert.ok(!text.includes('"runs"'));
    const bytes = JSON.stringify(canonical(geometry));
    assert.equal(createHash("sha256").update(bytes).digest("hex"),
        "e91ce3a27c011ef6b2549fdc36fa6e25db5c5da2d274233c9da4fc8adf4a0244");
});
