export const STATIC_WORLD_SEED = 0x41475731;
export const SCENE_HEIGHT = 640;
export const WORLD_SCALE = 10;
export const WORLD_ZERO_SCENE_Y = 348;
export const CHUNK_WIDTH = 50;
export const TERRAIN_SAMPLE_SPACING = 10;
export const RELIEF_SPAN = 320;
export const PLATFORM_WIDTH = 9.6;
export const PLATFORM_THICKNESS = 0.35;
export const PLATFORM_CLEARANCE = 2.4;
export const DECK_LEVEL = 116;
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

const TEMPLATE_SLOT_ORDER = Object.freeze([78, 93, 102]);

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

export function positiveModulo(value, modulus) {
    return ((value % modulus) + modulus) % modulus;
}

function smootherstep(value) {
    return value * value * value * (value * (value * 6 - 15) + 10);
}

export function terrainNormalizedKernel(seed, x) {
    const span = Math.floor(x / RELIEF_SPAN);
    const local = (x - span * RELIEF_SPAN) / RELIEF_SPAN;
    const leftAnchor = sampleUnit(seed, 13, span >>> 0);
    const rightAnchor = sampleUnit(seed, 13, (span + 1) >>> 0);
    const bias = sampleUnit(seed, 14, span >>> 0) - 0.5;
    const warped = local + bias * (smootherstep(local) - local);
    return 0.1 + 0.5 * (leftAnchor + (rightAnchor - leftAnchor) * smootherstep(warped));
}

export function terrainNormalizedSample(seed, sampleIndex) {
    return terrainNormalizedKernel(seed, sampleIndex * TERRAIN_SAMPLE_SPACING);
}

export function terrainNormalizedHeightAt(seed, x) {
    const leftIndex = Math.floor(x / TERRAIN_SAMPLE_SPACING);
    const leftX = leftIndex * TERRAIN_SAMPLE_SPACING;
    const fraction = (x - leftX) / TERRAIN_SAMPLE_SPACING;
    const left = terrainNormalizedSample(seed, leftIndex);
    const right = terrainNormalizedSample(seed, leftIndex + 1);
    return left + (right - left) * fraction;
}

export function terrainHeightAt(seed, x) {
    return 64 * terrainNormalizedHeightAt(seed, x) - 29.2;
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
    return siteScaffoldMembers(site).map(({ start, end }, index) => {
        const [startX, startY] = start;
        const [endX, endY] = end;
        if (index < 2) return `M${worldSceneX(startX)} ${worldSceneY(startY)}H${worldSceneX(endX)}`;
        return `M${worldSceneX(startX)} ${worldSceneY(startY)}L${worldSceneX(endX)} ${worldSceneY(endY)}`;
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
            vertices.push([x, terrainHeightAt(seed, x)]);
        }
    }
    if (right < last * TERRAIN_SAMPLE_SPACING) {
        vertices.push([right, terrainHeightAt(seed, right)]);
    }
    return vertices;
}

export function createFirstSite(seed) {
    const normalized = normalizeSeed(seed);
    const center = 36;
    const platformLeft = center - PLATFORM_WIDTH / 2;
    const platformRight = center + PLATFORM_WIDTH / 2;
    const deckLevel = DECK_LEVEL;
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
    const base = Math.floor(3 * sampleUnit(seed, 3, siteIndex));
    const preferred = [];
    for (let count = 0; count < 3; count += 1) {
        preferred.push(TEMPLATE_SLOT_ORDER[(base + 2 * count) % 3]);
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
        if (originSite.deckLevel === DECK_LEVEL && template.deckDelta === 0) {
            return template;
        }
    }
    throw new Error("No eligible route template");
}

export function instantiateTemplateSite(seed, siteIndex, originSite, template) {
    const center = originSite.center + template.centerDelta;
    const deckLevel = DECK_LEVEL;
    const platformTop = deckLevel / 10;
    if (originSite.deckLevel !== DECK_LEVEL || template.deckDelta !== 0) {
        throw new RangeError(`Route template ${template.templateId} does not use the canonical deck datum`);
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
    const vertices = [...points].sort((a, b) => a - b).map((x) => [x, terrainHeightAt(seed, x)]);
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

export function worldSceneX(worldX) {
    return Number((worldX * WORLD_SCALE).toFixed(12));
}

export function worldSceneY(worldY) {
    return WORLD_ZERO_SCENE_Y - worldY * WORLD_SCALE;
}

export function cameraLeftForPose(pose) {
    if (pose.x < 6.7) return pose.x - 6.7;
    if (pose.x > 33.3) return pose.x - 33.3;
    return 0;
}

export function cameraForPose(pose) {
    const preCameraHullTop = worldSceneY(pose.y + 6.7);
    return freeze({ left: cameraLeftForPose(pose), down: clamp(40 - preCameraHullTop, 0, 320) });
}

export function worldViewportX(worldX, camera) {
    return worldSceneX(worldX) - worldSceneX(camera.left);
}

export function worldViewportY(worldY, camera) {
    return worldSceneY(worldY) + camera.down;
}

export function worldGroupOffsetX(camera) {
    return worldSceneX(-camera.left);
}

export function worldGroupOffsetY(camera) {
    return camera.down;
}

export function targetDirectionForViewport(targetSite, cameraLeft) {
    if (!targetSite) return null;
    const buildingRight = siteStructure(targetSite).buildingRight;
    if (targetSite.platformLeft < cameraLeft) return "left";
    if (buildingRight > cameraLeft + 100) return "right";
    return null;
}

function skyWorldLeftForCamera(camera) {
    return camera.left * 0.24;
}

function skyChunkKeyForCamera(camera) {
    const firstChunk = Math.floor(skyWorldLeftForCamera(camera) / 50) - 1;
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

export function skyProjectionIdentityForCamera(seed, camera) {
    return `${normalizeSeed(seed)}|${skyChunkKeyForCamera(camera)}`;
}

export function skyProjectionForCamera(seed, camera) {
    const key = skyChunkKeyForCamera(camera);
    const chunks = key.split(":").map(Number);
    const stars = [];
    const landmarks = [];
    const landmarkOffset = Math.floor(4 * sampleUnit(seed, 8, 0));
    for (const chunk of chunks) {
        for (let index = 0; index < 4; index += 1) {
            const key = (Math.imul(chunk, 4) + index) >>> 0;
            const x = worldSceneX(chunk * 50 + 4 + 42 * sampleUnit(seed, 6, key));
            const y = 50 + 190 * sampleUnit(seed, 7, key);
            stars.push(`M${x} ${y}h2`);
        }
        if (positiveModulo(chunk - landmarkOffset, 4) !== 0) continue;
        const key = chunk >>> 0;
        const x = worldSceneX(chunk * 50 + 10 + 30 * sampleUnit(seed, 9, key));
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
    return freeze({ key, chunks, groupOffsetX: worldSceneX(-skyWorldLeftForCamera(camera)),
        starsPath: stars.join(""), landmarksPath: landmarks.join("") });
}

export function terrainSurfacePath(vertices) {
    if (vertices.length === 0) {
        return "";
    }
    const points = vertices.map(([x, y]) => `${worldSceneX(x)} ${worldSceneY(y)}`).join("L");
    return `M${points}`;
}

export function terrainFillPath(vertices) {
    if (vertices.length === 0) {
        return "";
    }
    return `${terrainSurfacePath(vertices)}L${worldSceneX(vertices.at(-1)[0])} 648L${worldSceneX(vertices[0][0])} 648Z`;
}
