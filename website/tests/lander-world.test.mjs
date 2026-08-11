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
    targetIsOffscreen,
    terrainFillPath,
    terrainHeightFromVertices,
    terrainHeightAt,
    terrainSurfacePath,
    terrainVerticesForRange,
    terrainVerticesForWindow,
} from "../static/lander-world.js";

const GEOMETRY_URL = new URL("fixtures/lander-route-geometry-v3.json", import.meta.url);
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
        for (const x of [site.platformLeft, site.platformLeft + 9.3, site.platformLeft + 9.6,
            site.platformLeft + 11.6, site.platformLeft + 18.6]) {
            assert.ok(vertices.some(([candidate]) => candidate === x), `missing native site sample ${x}`);
        }
        for (const { center } of structure.pylons) {
            close(terrainHeightFromVertices(vertices, center), terrainHeightAt(STATIC_WORLD_SEED, center));
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

test("site has one twelve-bay Warren truss and three independently footed pylons", () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    const structure = siteStructure(site);
    const members = siteScaffoldMembers(site);
    assert.equal(members.length, 17);
    assert.deepEqual(members.slice(0, 2).map(({ start, end }) => [start, end]), [
        [[site.platformLeft, site.platformBottom], [structure.buildingRight, site.platformBottom]],
        [[site.platformLeft, structure.trussBottom], [structure.buildingRight, structure.trussBottom]],
    ]);
    assert.equal(members.slice(2, 14).length, 12);
    assert.deepEqual(structure.pylons.map(({ center }) => center),
        [site.platformLeft, site.platformLeft + 9.3, structure.buildingRight]);
    structure.pylons.forEach((pylon) => close(pylon.foot, terrainHeightAt(site.seed, pylon.center)));
    assert.deepEqual(structure.truss, {
        bottom: site.platformBottom - 0.85,
        left: site.platformLeft - 0.1,
        right: structure.buildingRight + 0.1,
        top: site.platformBottom + 0.1,
    });
    const pylons = members.slice(14);
    assert.equal(pylons.length, 3);
    pylons.forEach(({ start, end }, index) => {
        const pylon = structure.pylons[index];
        assert.deepEqual(start, [pylon.center, site.platformBottom]);
        assert.notEqual(start[1], structure.trussBottom,
            "a pylon must pass through the bottom chord from the deck underside");
        assert.deepEqual(end, [pylon.center, pylon.foot]);
        assert.equal(pylon.collider.top, start[1] + 0.1);
        assert.equal(pylon.collider.bottom, end[1] - 0.1);
    });
    const path = siteScaffoldPath(site);
    assert.equal(path.match(/M/g)?.length, 17);
    assert.doesNotMatch(path, /Z/);
    const renderedPylons = [...path.matchAll(/M(-?[\d.]+) (-?[\d.]+)V(-?[\d.]+)/g)].map((match) =>
        match.slice(1).map(Number));
    assert.deepEqual(renderedPylons, pylons.map(({ start, end }) =>
        [Number((start[0] * 10).toFixed(12)), 548 - start[1] * 10, 548 - end[1] * 10]));
});

test("camera and rolling retention stay bounded", () => {
    const site = createFirstSite(STATIC_WORLD_SEED);
    assert.equal(cameraLeftForPose({ x: 34 }), 0);
    assert.equal(cameraLeftForPose({ x: 80 }), 45);
    assert.equal(CHUNK_WIDTH, 50);
    assert.ok(retainedChunkIndexes(45).length <= 5);
    assert.ok(retainedSiteDescriptors([site], 0, 0).length <= 3);
    assert.equal(targetIsOffscreen({ platformLeft: 200 }, 0), true);
});

test("geometry-v3 fixture is independent and has the approved canonical digest", async () => {
    const text = await readFile(GEOMETRY_URL, "utf8");
    const geometry = JSON.parse(text);
    assert.equal(geometry.schema, "agw-lander-route-geometry/v3");
    assert.equal(geometry.templates.length, 9);
    assert.equal(geometry.siteGeometry.truss.bayCount, 12);
    assert.deepEqual(geometry.siteGeometry.pylons.positions, [0, 9.3, 18.6]);
    assert.ok(!text.includes('"runs"'));
    assert.equal(createHash("sha256").update(JSON.stringify(canonical(geometry))).digest("hex"),
        "2cc7b145dc516426d911f2f51f47cc374f0154905d8ddff00cc78e141de14195");
});
