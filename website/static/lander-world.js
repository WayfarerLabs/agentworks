export const STATIC_WORLD_SEED = 0x41475731;
export const CHUNK_WIDTH = 50;
export const TERRAIN_SAMPLE_SPACING = 10;
export const PLATFORM_WIDTH = 9.6;
export const PLATFORM_THICKNESS = 0.35;
export const PLATFORM_CLEARANCE = 2.4;
export const DECK_LEVELS = Object.freeze([83, 91, 99]);
export const SCAFFOLD_MEMBER_WIDTH = 0.2;
export const SCAFFOLD_MEMBER_HALF = SCAFFOLD_MEMBER_WIDTH / 2;
export const TRUSS_BAY_COUNT = 12;
export const TRUSS_BAY_HEIGHT = 0.75;
export const TRUSS_BAY_WIDTH = 1.55;
export const COLUMN_WIDTH = 1;
export const COLUMN_BAY_HEIGHT = 0.8;
export const NOC_CONNECTOR_WIDTH = 2;
export const NOC_WIDTH = 7;
export const NOC_ROOF_OFFSET = 7.2;
export const NOC_MAST_WIDTH = 0.5;
export const NOC_MAST_HEIGHT = 3.2;

export const MOTIFS = Object.freeze([
    Object.freeze([0, 2.4, -1.5, 1.8, -1.1, 0]),
    Object.freeze([0, -2.1, -0.8, 2.2, 1, 0]),
    Object.freeze([0, 0.9, 2.5, 0.6, -1.9, 0]),
    Object.freeze([0, -1.4, 1.3, 2.4, -0.5, 0]),
]);
const TEMPLATE_SLOT_ORDER = Object.freeze([78, 81, 84, 87, 90, 93, 96, 99, 102]);

function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
}

function freeze(value) {
    if (Array.isArray(value)) {
        value.forEach(freeze);
    } else if (value && typeof value === "object") {
        Object.values(value).forEach(freeze);
    }
    return Object.freeze(value);
}

export function normalizeSeed(value) {
    const normalized = Number(value) >>> 0;
    return normalized === 0 ? 0x6d2b79f5 : normalized;
}

export function mixUint32(input) {
    let value = Number(input) >>> 0;
    value = Math.imul(value ^ (value >>> 16), 0x7feb352d);
    value = Math.imul(value ^ (value >>> 15), 0x846ca68b);
    return (value ^ (value >>> 16)) >>> 0;
}

export function sampleUnit(seed, stream, index) {
    const value =
        normalizeSeed(seed) ^
        Math.imul(stream, 0x9e3779b9) ^
        Math.imul((Number(index) + 1) >>> 0, 0x85ebca6b);
    return mixUint32(value) / 2 ** 32;
}

function boundaryHeight(seed, boundaryIndex) {
    return 1.5 + 4.5 * sampleUnit(seed, 1, boundaryIndex >>> 0);
}

export function positiveModulo(value, modulus) {
    return ((value % modulus) + modulus) % modulus;
}

export function motifSelection(seed) {
    return freeze({
        direction: sampleUnit(seed, 2, 1) < 0.5 ? 1 : 3,
        offset: Math.floor(4 * sampleUnit(seed, 2, 0)),
    });
}

export function motifIndex(seed, chunkIndex) {
    const selection = motifSelection(seed);
    return positiveModulo(selection.offset + selection.direction * chunkIndex, 4);
}

export function terrainSample(seed, sampleIndex) {
    const chunkIndex = Math.floor(sampleIndex / 5);
    const localIndex = sampleIndex - chunkIndex * 5;
    if (localIndex === 0) {
        return boundaryHeight(seed, chunkIndex);
    }
    const start = boundaryHeight(seed, chunkIndex);
    const end = boundaryHeight(seed, chunkIndex + 1);
    const base = start + (end - start) * (localIndex / 5);
    return clamp(base + MOTIFS[motifIndex(seed, chunkIndex)][localIndex], 0.5, 7.5);
}

export function terrainHeightAt(seed, x) {
    const leftIndex = Math.floor(x / TERRAIN_SAMPLE_SPACING);
    const leftX = leftIndex * TERRAIN_SAMPLE_SPACING;
    const fraction = (x - leftX) / TERRAIN_SAMPLE_SPACING;
    const left = terrainSample(seed, leftIndex);
    const right = terrainSample(seed, leftIndex + 1);
    return left + (right - left) * fraction;
}

export function terrainHeightFromVertices(vertices, x) {
    for (let index = 1; index < vertices.length; index += 1) {
        const left = vertices[index - 1]; const right = vertices[index];
        if (x < left[0] || x > right[0]) continue;
        if (right[0] === left[0]) return Math.min(left[1], right[1]);
        return left[1] + (right[1] - left[1]) * ((x - left[0]) / (right[0] - left[0]));
    }
    throw new RangeError(`Terrain vertices do not cover ${x}`);
}

export function terrainVerticesForRange(vertices, left, right) {
    if (vertices.length === 0 || right < left || right < vertices[0][0] || left > vertices.at(-1)[0]) {
        return freeze([]);
    }
    const clippedLeft = Math.max(left, vertices[0][0]);
    const clippedRight = Math.min(right, vertices.at(-1)[0]);
    if (clippedLeft === clippedRight) {
        const point = vertices.find(([x]) => x === clippedLeft);
        return freeze([[clippedLeft, point?.[1] ?? terrainHeightFromVertices(vertices, clippedLeft)]]);
    }
    const interior = vertices.filter(([x]) => x > clippedLeft && x < clippedRight);
    return freeze([
        [clippedLeft, terrainHeightFromVertices(vertices, clippedLeft)],
        ...interior.map((point) => [...point]),
        [clippedRight, terrainHeightFromVertices(vertices, clippedRight)],
    ]);
}

export function siteStructure(site) {
    const buildingLeft = site.platformRight + NOC_CONNECTOR_WIDTH;
    const buildingRight = buildingLeft + NOC_WIDTH;
    const roof = site.platformTop + NOC_ROOF_OFFSET;
    const trussBottom = site.platformBottom - TRUSS_BAY_HEIGHT;
    const supportColumns = [[0, 1], [8.8, 9.8], [17.6, 18.6]].map(([leftOffset, rightOffset], index) => {
        const left = site.platformLeft + leftOffset;
        const right = site.platformLeft + rightOffset;
        const leftFoot = terrainHeightAt(site.seed, left);
        const rightFoot = terrainHeightAt(site.seed, right);
        const latticeFloor = Math.max(leftFoot, rightFoot);
        const bayCount = Math.ceil((site.platformBottom - latticeFloor) / COLUMN_BAY_HEIGHT);
        const levels = Array.from({ length: bayCount + 1 }, (_, index) =>
            Math.max(latticeFloor, site.platformBottom - COLUMN_BAY_HEIGHT * index));
        return {
            bayCount,
            index,
            latticeFloor,
            left,
            leftFoot,
            levels: levels.filter((level, index) => index === 0 || level !== levels[index - 1]),
            right,
            rightFoot,
            collider: {
                bottom: Math.min(leftFoot, rightFoot) - SCAFFOLD_MEMBER_HALF,
                left: left - SCAFFOLD_MEMBER_HALF,
                right: right + SCAFFOLD_MEMBER_HALF,
                top: site.platformBottom + SCAFFOLD_MEMBER_HALF,
            },
        };
    });
    return freeze({
        buildingLeft,
        buildingRight,
        mast: {
            bottom: roof,
            left: buildingLeft + (NOC_WIDTH - NOC_MAST_WIDTH) / 2,
            right: buildingLeft + (NOC_WIDTH + NOC_MAST_WIDTH) / 2,
            top: roof + NOC_MAST_HEIGHT,
        },
        noc: { bottom: site.platformBottom, left: buildingLeft, right: buildingRight, top: roof },
        supportColumns,
        roof,
        truss: {
            bottom: trussBottom - SCAFFOLD_MEMBER_HALF,
            left: site.platformLeft - SCAFFOLD_MEMBER_HALF,
            right: buildingRight + SCAFFOLD_MEMBER_HALF,
            top: site.platformBottom + SCAFFOLD_MEMBER_HALF,
        },
        trussBottom,
    });
}

export function siteScaffoldMembers(site) {
    const structure = siteStructure(site);
    const bayWidth = (structure.buildingRight - site.platformLeft) / TRUSS_BAY_COUNT;
    const members = [
        { start: [site.platformLeft, site.platformBottom], end: [structure.buildingRight, site.platformBottom] },
        { start: [site.platformLeft, structure.trussBottom], end: [structure.buildingRight, structure.trussBottom] },
    ];
    for (let bay = 0; bay < TRUSS_BAY_COUNT; bay += 1) {
        const left = site.platformLeft + bayWidth * bay;
        const right = left + bayWidth;
        members.push(bay % 2 === 0 ?
            { start: [left, site.platformBottom], end: [right, structure.trussBottom] } :
            { start: [left, structure.trussBottom], end: [right, site.platformBottom] });
    }
    for (const column of structure.supportColumns) {
        members.push(
            { start: [column.left, site.platformBottom], end: [column.left, column.leftFoot] },
            { start: [column.right, site.platformBottom], end: [column.right, column.rightFoot] },
        );
        for (const level of column.levels) {
            members.push({ start: [column.left, level], end: [column.right, level] });
        }
        for (let bay = 0; bay < column.levels.length - 1; bay += 1) {
            const top = column.levels[bay];
            const bottom = column.levels[bay + 1];
            members.push(bay % 2 === 0 ?
                { start: [column.left, top], end: [column.right, bottom] } :
                { start: [column.right, top], end: [column.left, bottom] });
        }
    }
    return freeze(members.map((member) => ({ cap: "butt", join: "round", ...member })));
}

export function siteScaffoldPath(site) {
    const projectX = (x) => Number((x * 10).toFixed(12));
    const projectY = (y) => 548 - y * 10;
    return siteScaffoldMembers(site).map(({ start, end }, index) => {
        const [startX, startY] = start;
        const [endX, endY] = end;
        if (index < 2) return `M${projectX(startX)} ${projectY(startY)}H${projectX(endX)}`;
        return `M${projectX(startX)} ${projectY(startY)}L${projectX(endX)} ${projectY(endY)}`;
    }).join("");
}

export function nativeTerrainVertices(seed, left, right) {
    const first = Math.floor(left / TERRAIN_SAMPLE_SPACING);
    const last = Math.ceil(right / TERRAIN_SAMPLE_SPACING);
    const vertices = [];
    if (left > first * TERRAIN_SAMPLE_SPACING) {
        vertices.push([left, terrainHeightAt(seed, left)]);
    }
    for (let index = first; index <= last; index += 1) {
        const x = index * TERRAIN_SAMPLE_SPACING;
        if (x >= left && x <= right) {
            vertices.push([x, terrainSample(seed, index)]);
        }
    }
    if (right < last * TERRAIN_SAMPLE_SPACING) {
        vertices.push([right, terrainHeightAt(seed, right)]);
    }
    return vertices;
}

function maximumTerrain(seed, left, right) {
    return Math.max(...nativeTerrainVertices(seed, left, right).map((point) => point[1]));
}

function minimumDeckLevel(seed, center) {
    const platformLeft = center - PLATFORM_WIDTH / 2;
    const buildingRight = center + PLATFORM_WIDTH / 2 + NOC_CONNECTOR_WIDTH + NOC_WIDTH;
    const minimumTop = maximumTerrain(seed, platformLeft, buildingRight) + PLATFORM_CLEARANCE;
    return DECK_LEVELS.find((level) => level / 10 >= minimumTop);
}

export function createFirstSite(seed) {
    const normalized = normalizeSeed(seed);
    const center = 36;
    const platformLeft = center - PLATFORM_WIDTH / 2;
    const platformRight = center + PLATFORM_WIDTH / 2;
    const deckLevel = minimumDeckLevel(normalized, center);
    const platformTop = deckLevel / 10;
    return freeze({
        id: 0,
        seed: normalized,
        center,
        deckLevel,
        platformLeft,
        platformRight,
        platformTop,
        platformBottom: platformTop - PLATFORM_THICKNESS,
        canCollected: false,
        powered: false,
        nocStage: 0,
        templateId: "initial",
    });
}

export function templatePreference(seed, siteIndex) {
    const base = Math.floor(9 * sampleUnit(seed, 3, siteIndex));
    const preferred = [];
    for (let count = 0; count < 9; count += 1) {
        preferred.push(TEMPLATE_SLOT_ORDER[(base + 4 * count) % 9]);
    }
    return preferred;
}

export function selectTemplate(seed, siteIndex, originSite, templates) {
    const byDistance = new Map(templates.map((template) => [template.centerDelta, template]));
    for (const distance of templatePreference(seed, siteIndex)) {
        const template = byDistance.get(distance);
        if (!template) {
            throw new Error(`Missing route template for ${distance} metres`);
        }
        const targetLevel = originSite.deckLevel + Math.round(template.deckDelta * 10);
        const targetCenter = originSite.center + template.centerDelta;
        if (DECK_LEVELS.includes(targetLevel) && targetLevel >= minimumDeckLevel(seed, targetCenter)) {
            return template;
        }
    }
    throw new Error("No eligible route template");
}

function interpolateKnots(knots, x) {
    if (x <= knots[0][0]) {
        return knots[0][1];
    }
    for (let index = 1; index < knots.length; index += 1) {
        const right = knots[index];
        const left = knots[index - 1];
        if (x <= right[0]) {
            const fraction = (x - left[0]) / (right[0] - left[0]);
            return left[1] + (right[1] - left[1]) * fraction;
        }
    }
    return knots.at(-1)[1];
}

export function instantiateTemplateSite(seed, siteIndex, originSite, template) {
    const center = originSite.center + template.centerDelta;
    const deckLevel = originSite.deckLevel + Math.round(template.deckDelta * 10);
    const platformTop = deckLevel / 10;
    if (!DECK_LEVELS.includes(deckLevel) || deckLevel < minimumDeckLevel(seed, center)) {
        throw new RangeError(`Route template ${template.templateId} does not clear native terrain`);
    }
    return freeze({
        id: siteIndex,
        seed: normalizeSeed(seed),
        center,
        deckLevel,
        platformLeft: center - PLATFORM_WIDTH / 2,
        platformRight: center + PLATFORM_WIDTH / 2,
        platformTop,
        platformBottom: platformTop - PLATFORM_THICKNESS,
        canCollected: false,
        powered: false,
        nocStage: 0,
        templateId: template.templateId,
        originSiteId: originSite.id,
        clearanceKnots: template.clearanceKnots.map((point) => [...point]),
    });
}

export function corridorVertices(seed, originSite, targetSite) {
    const originRight = originSite.platformRight + NOC_CONNECTOR_WIDTH + NOC_WIDTH;
    const targetLeft = targetSite.platformLeft;
    const vertices = [];
    const firstIndex = Math.floor(originRight / TERRAIN_SAMPLE_SPACING) + 1;
    const lastIndex = Math.ceil(targetLeft / TERRAIN_SAMPLE_SPACING) - 1;
    for (let index = firstIndex; index <= lastIndex; index += 1) {
        const x = index * TERRAIN_SAMPLE_SPACING;
        const raw = terrainSample(seed, index);
        const relativeX = x - originSite.center;
        const cap = originSite.platformTop + interpolateKnots(targetSite.clearanceKnots, relativeX);
        const y = raw > cap ? Math.max(0.5, cap - 0.15 * sampleUnit(seed, 4, index >>> 0)) : raw;
        vertices.push([x, y]);
    }
    return vertices;
}

export function terrainVerticesForWindow(seed, sites, left, right) {
    const ordered = [...sites].sort((a, b) => a.center - b.center);
    const points = new Set(nativeTerrainVertices(seed, left, right).map(([x]) => x));
    for (const site of ordered) {
        [site.platformLeft, site.platformLeft + 1, site.platformLeft + 8.8,
            site.platformLeft + 9.6, site.platformLeft + 9.8, site.platformLeft + 11.6,
            site.platformLeft + 17.6, site.platformLeft + 18.6].forEach((x) => {
            if (x >= left && x <= right) points.add(x);
        });
    }
    const corridors = [];
    for (let index = 1; index < ordered.length; index += 1) {
        if (ordered[index].clearanceKnots) {
            const origin = ordered[index - 1];
            const target = ordered[index];
            corridors.push({ origin, target, byX: new Map(corridorVertices(seed, origin, target)) });
        }
    }
    const vertices = [...points].sort((a, b) => a - b).map((x) => {
        const corridor = corridors.find(({ origin, target }) =>
            x > origin.platformRight + NOC_CONNECTOR_WIDTH + NOC_WIDTH && x < target.platformLeft);
        return [x, corridor?.byX.get(x) ?? terrainHeightAt(seed, x)];
    });
    if (vertices.some((point, index) => index > 0 && vertices[index - 1][0] >= point[0])) {
        throw new Error("Terrain vertices must have strictly increasing X coordinates");
    }
    return freeze(vertices);
}

export function retainedChunkIndexes(cameraLeft) {
    const left = cameraLeft - 40;
    const right = cameraLeft + 140;
    const first = Math.floor(left / CHUNK_WIDTH);
    const last = Math.ceil(right / CHUNK_WIDTH) - 1;
    return freeze(Array.from({ length: last - first + 1 }, (_, offset) => first + offset));
}

export function retainedSiteDescriptors(sites, activeSiteId, targetSiteId) {
    const byId = new Map(sites.map((site) => [site.id, site]));
    const ids = new Set([activeSiteId, targetSiteId]);
    if (activeSiteId !== null) {
        ids.add(activeSiteId - 1);
    }
    return freeze(
        [...ids]
            .filter((id) => id !== null && byId.has(id))
            .sort((a, b) => a - b)
            .map((id) => byId.get(id)),
    );
}

export function cameraLeftForPose(pose) {
    if (pose.x < 5) return pose.x - 5;
    if (pose.x > 35) return pose.x - 35;
    return 0;
}

export function targetDirectionForViewport(targetSite, cameraLeft) {
    if (!targetSite) return null;
    if (targetSite.platformLeft > cameraLeft + 100) return "right";
    if (targetSite.platformRight < cameraLeft) return "left";
    return null;
}

function skyChunkKeyForCamera(cameraLeft) {
    const firstChunk = Math.floor(cameraLeft * 0.24 / 50) - 1;
    return Array.from({ length: 5 }, (_, index) => firstChunk + index).join(":");
}

const PLANET_RADIUS = 16;
const PLANET_RING_PROFILES = Object.freeze([
    Object.freeze([[28, 9]]),
    Object.freeze([[31, 10]]),
    Object.freeze([
        [28, 9],
        [34, 12],
    ]),
]);

function occludedRingPath(x, y, radiusX, radiusY) {
    const cutX = Math.sqrt((PLANET_RADIUS ** 2 - radiusY ** 2) / (1 - radiusY ** 2 / radiusX ** 2));
    const cutY = Math.sqrt(PLANET_RADIUS ** 2 - cutX ** 2);
    return (
        `M${x - radiusX} ${y}A${radiusX} ${radiusY} 0 0 1 ${x - cutX} ${y - cutY}` +
        `M${x + cutX} ${y - cutY}A${radiusX} ${radiusY} 0 0 1 ${x + radiusX} ${y}` +
        `M${x + radiusX} ${y}A${radiusX} ${radiusY} 0 0 1 ${x - radiusX} ${y}`
    );
}

export function skyProjectionIdentityForCamera(seed, cameraLeft) {
    return `${normalizeSeed(seed)}|${skyChunkKeyForCamera(cameraLeft)}`;
}

export function skyProjectionForCamera(seed, cameraLeft) {
    const key = skyChunkKeyForCamera(cameraLeft);
    const chunks = key.split(":").map(Number);
    const stars = [];
    const landmarks = [];
    const landmarkOffset = Math.floor(4 * sampleUnit(seed, 8, 0));
    for (const chunk of chunks) {
        for (let index = 0; index < 4; index += 1) {
            const key = (Math.imul(chunk, 4) + index) >>> 0;
            const x = (chunk * 50 + 4 + 42 * sampleUnit(seed, 6, key)) * 10;
            const y = 50 + 190 * sampleUnit(seed, 7, key);
            stars.push(`M${x} ${y}h2`);
        }
        if (positiveModulo(chunk - landmarkOffset, 4) !== 0) continue;
        const key = chunk >>> 0;
        const x = 10 * (chunk * 50 + 10 + 30 * sampleUnit(seed, 9, key));
        const y = 90 + 110 * sampleUnit(seed, 10, key);
        if (sampleUnit(seed, 11, key) < 0.5) {
            landmarks.push(`M${x} ${y - 18}A18 18 0 1 0 ${x} ${y + 18}A13 18 0 0 1 ${x} ${y - 18}`);
        } else {
            const profile = PLANET_RING_PROFILES[Math.floor(3 * sampleUnit(seed, 12, key))];
            landmarks.push(
                `M${x - PLANET_RADIUS} ${y}A${PLANET_RADIUS} ${PLANET_RADIUS} 0 1 0 ` +
                    `${x + PLANET_RADIUS} ${y}A${PLANET_RADIUS} ${PLANET_RADIUS} 0 1 0 ` +
                    `${x - PLANET_RADIUS} ${y}Z` +
                    profile.map(([radiusX, radiusY]) => occludedRingPath(x, y, radiusX, radiusY)).join(""),
            );
        }
    }
    return freeze({ key, chunks, starsPath: stars.join(""), landmarksPath: landmarks.join("") });
}

export function terrainSurfacePath(vertices) {
    if (vertices.length === 0) {
        return "";
    }
    const points = vertices.map(([x, y]) => `${x * 10} ${548 - y * 10}`).join("L");
    return `M${points}`;
}

export function terrainFillPath(vertices) {
    if (vertices.length === 0) {
        return "";
    }
    return `${terrainSurfacePath(vertices)}L${vertices.at(-1)[0] * 10} 648L${vertices[0][0] * 10} 648Z`;
}
