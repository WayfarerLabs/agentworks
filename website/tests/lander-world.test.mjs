import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
    CHUNK_WIDTH,
    DECK_LEVEL,
    RELIEF_SPAN,
    STATIC_WORLD_SEED,
    TERRAIN_SAMPLE_SPACING,
    cameraForPose,
    createFirstSite,
    instantiateTemplateSite,
    retainedChunkIndexes,
    retainedSiteDescriptors,
    sampleUnit,
    selectTemplate,
    siteScaffoldMembers,
    siteScaffoldPath,
    siteStructure,
    skyProjectionForCamera,
    targetDirectionForViewport,
    terrainFillPath,
    terrainHeightAt,
    terrainNormalizedHeightAt,
    terrainNormalizedKernel,
    terrainSurfacePath,
    terrainVerticesForRange,
    terrainVerticesForWindow,
    worldGroupOffsetX,
    worldGroupOffsetY,
    worldSceneX,
    worldSceneY,
    worldViewportX,
    worldViewportY,
} from "../static/lander-world.js";

const GEOMETRY_URL = new URL("fixtures/lander-route-geometry-v5.json", import.meta.url);
const DERIVED_URL = new URL("fixtures/lander-route-derived-v5.json", import.meta.url);
const TEMPLATE_URL = new URL("../templates/lander-game.html", import.meta.url);

function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") {
        return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
    }
    return value;
}

function digest(value) {
    return createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex");
}

function close(actual, expected, tolerance = 1e-12) {
    assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} differs from ${expected}`);
}

function smootherstep(value) {
    return value ** 3 * (value * (value * 6 - 15) + 10);
}

function independentKernel(seed, x) {
    const span = Math.floor(x / 320);
    const local = (x - span * 320) / 320;
    const left = sampleUnit(seed, 13, span >>> 0);
    const right = sampleUnit(seed, 13, (span + 1) >>> 0);
    const bias = sampleUnit(seed, 14, span >>> 0) - 0.5;
    const warped = local + bias * (smootherstep(local) - local);
    return 0.1 + 0.5 * (left + (right - left) * smootherstep(warped));
}

function independentSample(seed, index) {
    return independentKernel(seed, index * 10);
}

function independentNormalizedAt(seed, x) {
    const leftIndex = Math.floor(x / 10);
    const fraction = (x - leftIndex * 10) / 10;
    const left = independentSample(seed, leftIndex);
    return left + (independentSample(seed, leftIndex + 1) - left) * fraction;
}

test("canonical relief independently stays broad, bounded, and deterministic", () => {
    assert.equal(RELIEF_SPAN, 320);
    assert.equal(TERRAIN_SAMPLE_SPACING, 10);
    const corpus = [
        { seed: 11, left: -1280, right: 1280 },
        { seed: 39, left: -7360, right: -6080 },
        { seed: 41, left: 1600, right: 2880 },
        { seed: STATIC_WORLD_SEED, left: -640, right: 1280 },
    ];
    let minimum = Infinity;
    let maximum = -Infinity;
    for (const { seed, left, right } of corpus) {
        const samples = [];
        for (let x = left; x <= right; x += 10) {
            const expected = independentNormalizedAt(seed, x);
            close(terrainNormalizedKernel(seed, x), independentKernel(seed, x));
            close(terrainNormalizedHeightAt(seed, x), expected);
            close(terrainHeightAt(seed, x), 64 * expected - 29.2);
            close(worldSceneY(terrainHeightAt(seed, x)), 640 * (1 - expected));
            samples.push([x, expected]);
            minimum = Math.min(minimum, expected);
            maximum = Math.max(maximum, expected);
        }
        const grades = samples.slice(1).map((rightPoint, index) =>
            (rightPoint[1] - samples[index][1]) / 10);
        assert.ok(grades.every((grade) => Math.abs(grade) <= 0.00439453125 + 1e-15));
        const adjacentGradeDeltas = grades.slice(1).map((grade, index) => Math.abs(grade - grades[index]));
        const adjacentCurvatures = adjacentGradeDeltas.map((delta) => delta / TERRAIN_SAMPLE_SPACING);
        assert.ok(adjacentGradeDeltas.every((delta) => delta <= 0.0008985859292196934 + 1e-15));
        assert.ok(adjacentCurvatures.every((curvature) => curvature <= 0.00008985859292196934 + 1e-15));
        const reversals = [];
        let previousSign = 0;
        for (let index = 0; index < grades.length; index += 1) {
            const sign = Math.sign(grades[index]);
            if (sign && previousSign && sign !== previousSign) reversals.push(samples[index][0]);
            if (sign) previousSign = sign;
        }
        assert.ok(reversals.slice(1).every((x, index) => x - reversals[index] >= 320));
        const backward = [];
        for (let x = right; x >= left; x -= 10) backward.unshift([x, independentNormalizedAt(seed, x)]);
        assert.deepEqual(backward, samples);
    }
    assert.ok(minimum <= 0.11, `minimum ${minimum}`);
    assert.ok(maximum >= 0.59, `maximum ${maximum}`);
    close(terrainNormalizedHeightAt(11, -640), 0.10337466620840133);
    close(terrainNormalizedHeightAt(41, 1920), 0.5986480843508616);
    close(terrainNormalizedHeightAt(STATIC_WORLD_SEED, 0), 0.4085759765235707);
    close(terrainNormalizedHeightAt(STATIC_WORLD_SEED, 100), 0.4399995140650219);
});

test("one strict sampled chain owns rendering, collision, and support feet", async () => {
    const geometry = JSON.parse(await readFile(GEOMETRY_URL, "utf8"));
    const origin = createFirstSite(STATIC_WORLD_SEED);
    const target = instantiateTemplateSite(
        STATIC_WORLD_SEED,
        1,
        origin,
        selectTemplate(STATIC_WORLD_SEED, 1, origin, geometry.templates),
    );
    const vertices = terrainVerticesForWindow(STATIC_WORLD_SEED, [origin, target], -40, 180);
    assert.ok(vertices.every((point, index) => index === 0 || vertices[index - 1][0] < point[0]));
    for (const site of [origin, target]) {
        for (const column of siteStructure(site).supportColumns) {
            const byX = new Map(vertices);
            close(byX.get(column.left), column.leftFoot);
            close(byX.get(column.right), column.rightFoot);
        }
    }
    const fill = terrainFillPath(vertices);
    const surface = terrainSurfacePath(vertices);
    assert.match(fill, /Z$/);
    assert.doesNotMatch(surface, /[ZV]/);
    assert.equal(surface.match(/M/g)?.length, 1);
    const clipped = terrainVerticesForRange(vertices, 0, 100);
    assert.equal(clipped[0][0], 0);
    assert.equal(clipped.at(-1)[0], 100);
});

test("fixed deck selection terminates in one check through one hundred sites", async () => {
    const geometry = JSON.parse(await readFile(GEOMETRY_URL, "utf8"));
    assert.equal(DECK_LEVEL, 116);
    const expectedCounts = new Map([
        [11, [31, 30, 38]],
        [39, [37, 29, 33]],
        [41, [34, 33, 32]],
        [STATIC_WORLD_SEED, [29, 33, 37]],
    ]);
    for (const [seed, expected] of expectedCounts) {
        let site = createFirstSite(seed);
        const counts = new Map(geometry.templates.map(({ templateId }) => [templateId, 0]));
        for (let index = 1; index < 100; index += 1) {
            const template = selectTemplate(seed, index, site, geometry.templates);
            counts.set(template.templateId, counts.get(template.templateId) + 1);
            site = instantiateTemplateSite(seed, index, site, template);
            assert.equal(site.deckLevel, 116);
        }
        assert.deepEqual([...counts.values()], expected);
    }
});

test("variable lattice columns join the fixed truss at canonical terrain feet", () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    const structure = siteStructure(site);
    const members = siteScaffoldMembers(site);
    assert.ok(members.length >= 41 && members.length <= 281);
    assert.equal(members.slice(2, 14).length, 12);
    let index = 14;
    for (const column of structure.supportColumns) {
        assert.ok(column.bayCount >= 3 && column.bayCount <= 43);
        const count = 3 + 2 * column.bayCount;
        assert.ok(count >= 9 && count <= 89);
        const columnMembers = members.slice(index, index + count);
        index += count;
        assert.deepEqual(columnMembers[0].end, [column.left, column.leftFoot]);
        assert.deepEqual(columnMembers[1].end, [column.right, column.rightFoot]);
        assert.equal(column.collider.bottom, Math.min(column.leftFoot, column.rightFoot) - 0.1);
    }
    assert.equal(index, members.length);
    assert.equal(siteScaffoldPath(site).match(/M/g)?.length, members.length);
});

test("shared horizontal and vertical projection keeps hull and target feasible", () => {
    const initial = cameraForPose({ x: 30, y: 32 });
    assert.deepEqual(initial, { left: 0, down: 79 });
    assert.equal(worldSceneX(12.5), 125);
    assert.equal(worldSceneY(11.6), 232);
    assert.equal(worldViewportY(11.6, initial), 311);
    assert.equal(worldGroupOffsetX(initial), 0);
    assert.equal(worldGroupOffsetY(initial), 79);
    const ceiling = cameraForPose({ x: 80, y: 56 });
    assert.deepEqual(ceiling, { left: 46.7, down: 319 });
    assert.equal(worldViewportX(46.7, ceiling), 0);
    assert.equal(worldViewportY(11.6, ceiling), 551);
    assert.equal(worldViewportY(11.25, ceiling), 554.5);
    assert.equal(worldViewportY(18.8, ceiling), 479);
    assert.equal(worldViewportY(22, ceiling), 447);
});

test("retention, cue, and sky remain bounded around frozen camera objects", () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    const camera = cameraForPose({ x: -20, y: 32 });
    assert.equal(camera.left, -26.7);
    assert.equal(CHUNK_WIDTH, 50);
    assert.ok(retainedChunkIndexes(camera.left).length <= 5);
    assert.ok(retainedSiteDescriptors([site], 0, 0).length <= 3);
    const target = { ...site, platformLeft: 99, platformRight: 108.6 };
    assert.equal(targetDirectionForViewport(target, 0), "right");
    assert.equal(targetDirectionForViewport(target, 98.9), null);
    assert.equal(targetDirectionForViewport(target, 100), "left");
    const sky = skyProjectionForCamera(STATIC_WORLD_SEED, cameraForPose({ x: 30, y: 32 }));
    assert.equal(sky.chunks.length, 5);
    assert.equal((sky.starsPath.match(/h2/g) ?? []).length, 20);
    assert.ok((sky.landmarksPath.match(/M/g) ?? []).length >= 1);
});

test("v5 fixtures and static markup project the same canonical world", async () => {
    const geometry = JSON.parse(await readFile(GEOMETRY_URL, "utf8"));
    const derived = JSON.parse(await readFile(DERIVED_URL, "utf8"));
    assert.equal(geometry.schema, "agw-lander-route-geometry/v5");
    assert.equal(geometry.templates.length, 3);
    assert.equal(derived.schema, "agw-lander-route-derived/v5");
    assert.equal(derived.worldWitnesses.length, 27);
    assert.equal(digest(geometry), derived.geometryDigest);
    assert.equal(derived.physicsDigest, "e08f8260b723dd245db88de9ae2cdbac54bf9a97cb0bed1b6f170eda362c48dc");
    for (const witness of derived.worldWitnesses) {
        const { terrain } = witness.descriptor;
        assert.deepEqual(terrain.normalizedVertices,
            terrain.worldVertices.map(([x]) => [x, terrainNormalizedHeightAt(witness.descriptor.seed, x)]));
        terrain.normalizedVertices.forEach(([x, normalized]) =>
            close(normalized, independentNormalizedAt(witness.descriptor.seed, x)));
        const grades = terrain.normalizedVertices.slice(1).map((right, index) =>
            (right[1] - terrain.normalizedVertices[index][1]) /
            (right[0] - terrain.normalizedVertices[index][0]));
        assert.deepEqual(terrain.segmentGrades, grades);
        assert.deepEqual(terrain.adjacentGradeChanges, grades.slice(1).map((grade, index) =>
            Math.abs(grade - grades[index]) /
            (terrain.normalizedVertices[index + 1][0] - terrain.normalizedVertices[index][0])));
        const signs = grades.map(Math.sign).filter(Boolean);
        assert.equal(terrain.reversalCount,
            signs.slice(1).filter((sign, index) => sign !== signs[index]).length);
        for (const span of terrain.spans) {
            assert.deepEqual(span, {
                bias: sampleUnit(witness.descriptor.seed, 14, span.span >>> 0) - 0.5,
                leftAnchor: sampleUnit(witness.descriptor.seed, 13, span.span >>> 0),
                rightAnchor: sampleUnit(witness.descriptor.seed, 13, (span.span + 1) >>> 0),
                span: span.span,
            });
        }
    }
    const template = await readFile(TEMPLATE_URL, "utf8");
    const site = createFirstSite(STATIC_WORLD_SEED);
    const vertices = terrainVerticesForWindow(STATIC_WORLD_SEED, [site], 0, 100);
    assert.ok(template.includes(`d="${terrainFillPath(vertices)}"`));
    assert.ok(template.includes(`d="${terrainSurfacePath(vertices)}"`));
    assert.ok(template.includes(`d="${siteScaffoldPath(site)}"`));
});
