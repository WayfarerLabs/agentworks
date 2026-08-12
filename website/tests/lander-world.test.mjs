import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
    CHUNK_WIDTH,
    DECK_LEVELS,
    STATIC_WORLD_SEED,
    cameraLeftForPose,
    createFirstSite,
    instantiateTemplateSite,
    retainedChunkIndexes,
    retainedSiteDescriptors,
    selectTemplate,
    siteScaffoldMembers,
    siteScaffoldPath,
    siteStructure,
    skyProjectionForCamera,
    targetDirectionForViewport,
    terrainFillPath,
    terrainHeightFromVertices,
    terrainHeightAt,
    terrainSurfacePath,
    terrainVerticesForRange,
    terrainVerticesForWindow,
} from "../static/lander-world.js";

const GEOMETRY_URL = new URL("fixtures/lander-route-geometry-v4.json", import.meta.url);
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

test("native terrain is retained beneath every site with strict x authority", async () => {
    const geometry = JSON.parse(await readFile(GEOMETRY_URL, "utf8"));
    const origin = createFirstSite(STATIC_WORLD_SEED);
    const template = selectTemplate(STATIC_WORLD_SEED, 1, origin, geometry.templates);
    const target = instantiateTemplateSite(STATIC_WORLD_SEED, 1, origin, template);
    const vertices = terrainVerticesForWindow(STATIC_WORLD_SEED, [origin, target], -40, target.center + 50);
    assert.ok(vertices.every((point, index) => index === 0 || vertices[index - 1][0] < point[0]));
    for (const site of [origin, target]) {
        const structure = siteStructure(site);
        for (const x of [site.platformLeft, site.platformLeft + 1, site.platformLeft + 8.8,
            site.platformLeft + 9.6, site.platformLeft + 9.8, site.platformLeft + 11.6,
            site.platformLeft + 17.6, site.platformLeft + 18.6]) {
            assert.ok(vertices.some(([candidate]) => candidate === x), `missing native site sample ${x}`);
        }
        for (const column of structure.supportColumns) {
            close(terrainHeightFromVertices(vertices, column.left), column.leftFoot);
            close(terrainHeightFromVertices(vertices, column.right), column.rightFoot);
        }
    }
});

test("terrain projects as one unstroked closed fill and one open stroked surface", async () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    const vertices = terrainVerticesForWindow(STATIC_WORLD_SEED, [site], -40, 140);
    const fill = terrainFillPath(vertices);
    const surface = terrainSurfacePath(vertices);
    assert.match(fill, /Z$/);
    assert.doesNotMatch(surface, /[ZV]/);
    assert.equal(surface.match(/M/g)?.length, 1);
    assert.equal(fill.match(/M/g)?.length, 1);
    const template = await readFile(TEMPLATE_URL, "utf8");
    const layer = template.match(/<g id="terrain-layer">([\s\S]*?)<\/g>/)?.[1] ?? "";
    assert.equal((layer.match(/<path/g) ?? []).length, 2);
    assert.match(layer, /class="terrain-fill"[^>]+fill="#d7d2c4"[^>]+stroke="none"/);
    assert.match(layer, /class="terrain-surface"[^>]+fill="none"[^>]+stroke="#4b4e55"/);
    assert.doesNotMatch(layer.match(/class="terrain-surface"[^>]+d="([^"]+)"/)?.[1] ?? "", /[ZV]/);
    assert.equal(terrainVerticesForRange(vertices, -40, 10).at(-1)[0], 10);
});

test("terrain range clipping is strict for degenerate, boundary, and outside ranges", () => {
    const vertices = Object.freeze([[0, 2], [40, 4], [64, 4], [100, 3]]
        .map((point) => Object.freeze(point)));
    const cases = [
        [-10, -1, []],
        [101, 110, []],
        [70, 30, []],
        [-10, 0, [[0, 2]]],
        [100, 110, [[100, 3]]],
        [40, 40, [[40, 4]]],
        [50, 50, [[50, 4]]],
        [30, 70, [[30, 3.5], [40, 4], [64, 4], [70, 3.8333333333333335]]],
    ];
    for (const [left, right, expected] of cases) {
        const clipped = terrainVerticesForRange(vertices, left, right);
        assert.deepEqual(clipped, expected, `${left}..${right}`);
        assert.ok(clipped.every((point, index) => index === 0 || clipped[index - 1][0] < point[0]),
            `${left}..${right} must remain strict in x`);
    }
});

test("integer deck tiers terminate exactly through one hundred sites for all witness seeds", async () => {
    const geometry = JSON.parse(await readFile(GEOMETRY_URL, "utf8"));
    assert.deepEqual(DECK_LEVELS, [83, 91, 99]);
    const first = createFirstSite(STATIC_WORLD_SEED);
    assert.equal(first.deckLevel, 99);
    assert.equal(first.platformTop, 9.9);
    for (const seed of [11, 39, 41, STATIC_WORLD_SEED]) {
        let site = createFirstSite(seed);
        for (let index = 1; index <= 100; index += 1) {
            const template = selectTemplate(seed, index, site, geometry.templates);
            site = instantiateTemplateSite(seed, index, site, template);
            assert.ok(DECK_LEVELS.includes(site.deckLevel));
        }
        assert.equal(site.id, 100);
    }
});

test("site has one twelve-bay Warren truss and three independently footed lattice columns", () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    const structure = siteStructure(site);
    const members = siteScaffoldMembers(site);
    assert.ok(members.length >= 41 && members.length <= 95);
    assert.deepEqual(members.slice(0, 2).map(({ start, end }) => [start, end]), [
        [[site.platformLeft, site.platformBottom], [structure.buildingRight, site.platformBottom]],
        [[site.platformLeft, structure.trussBottom], [structure.buildingRight, structure.trussBottom]],
    ]);
    assert.equal(members.slice(2, 14).length, 12);
    assert.deepEqual(structure.supportColumns.map(({ left, right }) => [left, right]), [
        [site.platformLeft, site.platformLeft + 1],
        [site.platformLeft + 8.8, site.platformLeft + 9.8],
        [site.platformLeft + 17.6, structure.buildingRight],
    ]);
    assert.deepEqual(structure.truss, {
        bottom: site.platformBottom - 0.85,
        left: site.platformLeft - 0.1,
        right: structure.buildingRight + 0.1,
        top: site.platformBottom + 0.1,
    });
    let memberIndex = 14;
    for (const column of structure.supportColumns) {
        const columnMembers = members.slice(memberIndex, memberIndex + 3 + 2 * column.bayCount);
        memberIndex += columnMembers.length;
        assert.deepEqual(columnMembers[0], { cap: "butt", join: "round",
            start: [column.left, site.platformBottom], end: [column.left, column.leftFoot] });
        assert.deepEqual(columnMembers[1], { cap: "butt", join: "round",
            start: [column.right, site.platformBottom], end: [column.right, column.rightFoot] });
        assert.ok(column.levels.slice(1).every((level, index) => column.levels[index] > level));
        assert.ok(column.levels.slice(1).every((level, index) => column.levels[index] - level <= 0.8 + 1e-12));
        assert.equal(column.collider.left, column.left - 0.1);
        assert.equal(column.collider.right, column.right + 0.1);
        assert.equal(column.collider.bottom, Math.min(column.leftFoot, column.rightFoot) - 0.1);
    }
    assert.equal(memberIndex, members.length);
    const path = siteScaffoldPath(site);
    assert.equal(path.match(/M/g)?.length, members.length);
    assert.doesNotMatch(path, /Z/);
});

test("camera and rolling retention stay bounded", () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    assert.equal(cameraLeftForPose({ x: 34 }), 0);
    assert.equal(cameraLeftForPose({ x: 80 }), 45);
    assert.equal(cameraLeftForPose({ x: -20 }), -25);
    assert.equal(CHUNK_WIDTH, 50);
    assert.ok(retainedChunkIndexes(45).length <= 5);
    assert.ok(retainedSiteDescriptors([site], 0, 0).length <= 3);
    assert.equal(targetDirectionForViewport({ platformLeft: 200, platformRight: 210 }, 0), "right");
    assert.equal(targetDirectionForViewport({ platformLeft: -20, platformRight: -10 }, 0), "left");
    assert.equal(targetDirectionForViewport({ platformLeft: 99, platformRight: 110 }, 0), null);
    const sky = skyProjectionForCamera(STATIC_WORLD_SEED, 0);
    assert.equal(sky.chunks.length, 5);
    assert.equal((sky.starsPath.match(/h2/g) ?? []).length, 20);
    assert.ok((sky.landmarksPath.match(/M/g) ?? []).length >= 1);
});

test("geometry-v4 fixture is independent and has the approved canonical digest", async () => {
    const text = await readFile(GEOMETRY_URL, "utf8");
    const geometry = JSON.parse(text);
    assert.equal(geometry.schema, "agw-lander-route-geometry/v4");
    assert.equal(geometry.templates.length, 9);
    assert.equal(geometry.siteGeometry.truss.bayCount, 12);
    assert.deepEqual(geometry.siteGeometry.supportColumns.railPairOffsets,
        [[0, 1], [8.8, 9.8], [17.6, 18.6]]);
    assert.ok(!text.includes('"runs"'));
    assert.equal(createHash("sha256").update(JSON.stringify(canonical(geometry))).digest("hex"),
        "e65792f7719e9e721089401bc5ab49206a26082cfe41676a5dd291177a62699a");
});
