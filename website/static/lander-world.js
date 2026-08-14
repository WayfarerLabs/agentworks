export const STATIC_WORLD_SEED = 0x41475731;
export const CHUNK_WIDTH = 50;
export const PLATFORM_WIDTH = 9.6;
export const PLATFORM_THICKNESS = 0.35;
export const PLATFORM_CLEARANCE = 2.5;
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

export const TERRAIN_VERTEX_CADENCE = 16;
export const TERRAIN_BLOCK_WIDTH = 128;
export const TERRAIN_GRADE_LIMIT = 0.36;
export const TERRAIN_GRADE_CHANGE_LIMIT = 0.4;
export const TERRAIN_NORMALIZED_MINIMUM = 0.1;
export const TERRAIN_NORMALIZED_MAXIMUM = 0.6;
export const SITE_SPACING = 96;

export const TERRAIN_PROFILES = freeze({
    H0: [0.35, 0.4, 0.48, 0.55, 0.58, 0.54, 0.46, 0.38, 0.35],
    H1: [0.35, 0.42, 0.5, 0.56, 0.59, 0.53, 0.44, 0.37, 0.35],
    H2: [0.35, 0.39, 0.47, 0.54, 0.57, 0.52, 0.45, 0.39, 0.35],
    H3: [0.35, 0.41, 0.49, 0.57, 0.6, 0.55, 0.47, 0.39, 0.35],
    L0: [0.35, 0.29, 0.21, 0.15, 0.12, 0.17, 0.25, 0.32, 0.35],
    L1: [0.35, 0.28, 0.2, 0.14, 0.1, 0.16, 0.24, 0.31, 0.35],
    L2: [0.35, 0.3, 0.23, 0.16, 0.13, 0.18, 0.26, 0.33, 0.35],
    L3: [0.35, 0.27, 0.19, 0.13, 0.11, 0.15, 0.23, 0.3, 0.35],
});

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
        normalizeSeed(seed) ^ Math.imul(stream, 0x9e3779b9) ^ Math.imul((Number(index) + 1) >>> 0, 0x85ebca6b);
    return mixUint32(value) / 2 ** 32;
}

export function positiveModulo(value, modulus) {
    return ((value % modulus) + modulus) % modulus;
}

export function terrainParityForSeed(seed) {
    return Math.floor(2 * sampleUnit(seed, 13, 0));
}

export function terrainCycleForSeed(seed) {
    return freeze({ blockWidth: TERRAIN_BLOCK_WIDTH, parity: terrainParityForSeed(seed), phases: [4, 36, 68, 100] });
}

export function terrainProfileForBlock(seed, blockIndex) {
    if (!Number.isSafeInteger(blockIndex)) throw new TypeError("Terrain block index must be a safe integer");
    const family = positiveModulo(blockIndex + terrainParityForSeed(seed), 2) === 0 ? "H" : "L";
    const variant = Math.floor(4 * sampleUnit(seed, 14, blockIndex >>> 0));
    const id = `${family}${variant}`;
    return freeze({ blockIndex, family, id, samples: TERRAIN_PROFILES[id], variant });
}

export function terrainHeightAt(seed, x) {
    if (!Number.isFinite(x)) throw new TypeError("Terrain X must be finite");
    const blockIndex = Math.floor(x / TERRAIN_BLOCK_WIDTH);
    const localX = x - blockIndex * TERRAIN_BLOCK_WIDTH;
    const segment = Math.min(7, Math.floor(localX / TERRAIN_VERTEX_CADENCE));
    const fraction = (localX - segment * TERRAIN_VERTEX_CADENCE) / TERRAIN_VERTEX_CADENCE;
    const samples = terrainProfileForBlock(seed, blockIndex).samples;
    const normalized = samples[segment] + (samples[segment + 1] - samples[segment]) * fraction;
    return 64 * normalized - 9.2;
}

export function terrainHeightFromVertices(vertices, x) {
    for (let index = 1; index < vertices.length; index += 1) {
        const left = vertices[index - 1];
        const right = vertices[index];
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
    const supportColumns = [
        [0, 1],
        [8.8, 9.8],
        [17.6, 18.6],
    ].map(([leftOffset, rightOffset], index) => {
        const left = site.platformLeft + leftOffset;
        const right = site.platformLeft + rightOffset;
        const leftFoot = site.supportFeet?.[index * 2] ?? terrainHeightAt(site.seed, left);
        const rightFoot = site.supportFeet?.[index * 2 + 1] ?? terrainHeightAt(site.seed, right);
        const latticeFloor = Math.max(leftFoot, rightFoot);
        const bayCount = Math.ceil((site.platformBottom - latticeFloor) / COLUMN_BAY_HEIGHT);
        const levels = Array.from({ length: bayCount + 1 }, (_, index) =>
            Math.max(latticeFloor, site.platformBottom - COLUMN_BAY_HEIGHT * index),
        );
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
        members.push(
            bay % 2 === 0
                ? { start: [left, site.platformBottom], end: [right, structure.trussBottom] }
                : { start: [left, structure.trussBottom], end: [right, site.platformBottom] },
        );
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
            members.push(
                bay % 2 === 0
                    ? { start: [column.left, top], end: [column.right, bottom] }
                    : { start: [column.right, top], end: [column.left, bottom] },
            );
        }
    }
    return freeze(members.map((member) => ({ cap: "butt", join: "round", ...member })));
}

export function siteScaffoldPath(site) {
    const projectX = (x) => Number((x * 10).toFixed(12));
    const projectY = (y) => 548 - y * 10;
    return siteScaffoldMembers(site)
        .map(({ start, end }, index) => {
            const [startX, startY] = start;
            const [endX, endY] = end;
            if (index < 2) return `M${projectX(startX)} ${projectY(startY)}H${projectX(endX)}`;
            return `M${projectX(startX)} ${projectY(startY)}L${projectX(endX)} ${projectY(endY)}`;
        })
        .join("");
}

export function createSiteForIndex(seed, siteIndex, state = {}) {
    if (!Number.isSafeInteger(siteIndex)) throw new TypeError("Site index must be a safe integer");
    const normalized = normalizeSeed(seed);
    const center = 36 + SITE_SPACING * siteIndex;
    const platformLeft = center - PLATFORM_WIDTH / 2;
    const platformRight = center + PLATFORM_WIDTH / 2;
    const buildingRight = center + 13.8;
    const candidates = [platformLeft, buildingRight];
    for (
        let x = Math.ceil(platformLeft / TERRAIN_VERTEX_CADENCE) * TERRAIN_VERTEX_CADENCE;
        x <= buildingRight;
        x += TERRAIN_VERTEX_CADENCE
    )
        candidates.push(x);
    const localNativeMaximum = Math.max(...candidates.map((x) => terrainHeightAt(normalized, x)));
    const platformTop = localNativeMaximum + PLATFORM_CLEARANCE;
    const supportXs = [
        platformLeft,
        platformLeft + 1,
        platformLeft + 8.8,
        platformLeft + 9.8,
        platformLeft + 17.6,
        platformLeft + 18.6,
    ];
    return freeze({
        id: siteIndex,
        seed: normalized,
        center,
        deckLevel: platformTop * 10,
        localNativeMaximum,
        supportFeet: supportXs.map((x) => terrainHeightAt(normalized, x)),
        platformLeft,
        platformRight,
        platformTop,
        platformBottom: platformTop - PLATFORM_THICKNESS,
        canCollected: state.canCollected ?? false,
        powered: state.powered ?? false,
        nocStage: state.nocStage ?? 0,
        pairKey: state.pairKey ?? null,
        originSiteId: state.originSiteId ?? null,
    });
}

export function createFirstSite(seed) {
    return createSiteForIndex(seed, 0);
}

function deckMillimeters(deck) {
    const result = Math.round(deck * 1000);
    if (Math.abs(result / 1000 - deck) > 1e-12) throw new Error(`Deck ${deck} is not millimetre exact`);
    return result;
}

export function routePairKey(originSite, targetSite) {
    return `d:${deckMillimeters(originSite.platformTop)}:${deckMillimeters(targetSite.platformTop)}`;
}

export function selectRouteProof(originSite, targetSite, proofCatalog) {
    if (targetSite.id !== originSite.id + 1 || targetSite.center - originSite.center !== SITE_SPACING) {
        throw new Error(`Sites ${originSite.id}/${targetSite.id} are not one forward route leg`);
    }
    const key = routePairKey(originSite, targetSite);
    const proof = proofCatalog[key];
    if (!proof || proof.pairKey !== key) throw new Error(`Missing exact route proof ${key}`);
    return proof;
}


export function terrainSiteForIndex(seed, siteIndex) {
    const site = createSiteForIndex(seed, siteIndex);
    return freeze({
        index: siteIndex,
        center: site.center,
        deckLevel: site.deckLevel,
        platformTop: site.platformTop,
        localNativeMaximum: site.localNativeMaximum,
        centerDelta: SITE_SPACING,
        phase: positiveModulo(site.center, TERRAIN_BLOCK_WIDTH),
    });
}

export function terrainVerticesForWindow(seed, sites, left, right) {
    if (!Number.isFinite(left) || !Number.isFinite(right) || right < left) {
        throw new RangeError("Terrain range must be finite and ordered");
    }
    const points = new Set([left, right]);
    for (
        let x = Math.ceil(left / TERRAIN_VERTEX_CADENCE) * TERRAIN_VERTEX_CADENCE;
        x <= right;
        x += TERRAIN_VERTEX_CADENCE
    )
        points.add(x);
    for (const site of sites) {
        [
            site.platformLeft,
            site.platformLeft + 1,
            site.platformLeft + 8.8,
            site.platformLeft + 9.6,
            site.platformLeft + 9.8,
            site.platformLeft + 11.6,
            site.platformLeft + 17.6,
            site.platformLeft + 18.6,
        ].forEach((x) => {
            if (x >= left && x <= right) points.add(x);
        });
    }
    const vertices = [...points].sort((a, b) => a - b).map((x) => [x, terrainHeightAt(seed, x)]);
    if (vertices.some((point, index) => index > 0 && vertices[index - 1][0] >= point[0])) {
        throw new Error("Terrain vertices must have strictly increasing X coordinates");
    }
    if (vertices.length > 48) throw new Error(`Terrain projection exceeds 48 vertices: ${vertices.length}`);
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
    const firstChunk = Math.floor((cameraLeft * 0.24) / 50) - 1;
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

export const MAX_PLAYABLE_Y = 56;
export const MAX_LANDING_HORIZONTAL_SPEED = 2.2;
export const MAX_LANDING_DESCENT_SPEED = 3.6;
export const MAX_LANDING_ANGLE = 18;
export const MAX_LANDING_ANGULAR_SPEED = 26;
export const COLLISION_MARGIN = 0.02;

const COLLISION_STEP_SECONDS = 1 / 120;
export const LANDER_HULL = Object.freeze([
    [-1.6, 0],
    [1.6, 0],
    [1.6, 6.5],
    [-1.6, 6.5],
]);
const HULL_RADIUS = Math.hypot(1.6, 6.5);
const collisionClamp = (value, minimum, maximum) => Math.min(maximum, Math.max(minimum, value));
const collisionSiteById = (model, id) => model.retainedSites.find((site) => site.id === id) ?? null;

export function normalizeDegrees(degrees) {
    return ((((degrees + 180) % 360) + 360) % 360) - 180;
}

export function transformLocalPoint(pose, localX, localY) {
    const radians = (pose.angle * Math.PI) / 180;
    return {
        x: pose.x + localX * Math.cos(radians) + localY * Math.sin(radians),
        y: pose.y - localX * Math.sin(radians) + localY * Math.cos(radians),
    };
}
function interpolatePose(left, right, fraction) {
    const lerp = (a, b) => a + (b - a) * fraction;
    return {
        x: lerp(left.x, right.x),
        y: lerp(left.y, right.y),
        vx: lerp(left.vx, right.vx),
        vy: lerp(left.vy, right.vy),
        angle: normalizeDegrees(left.angle + normalizeDegrees(right.angle - left.angle) * fraction),
        angularVelocity: lerp(left.angularVelocity, right.angularVelocity),
    };
}

export function hullForPose(pose) {
    return LANDER_HULL.map(([x, y]) => transformLocalPoint(pose, x, y));
}

export function hullBounds(pose) {
    const radians = (pose.angle * Math.PI) / 180;
    const sine = Math.sin(radians);
    const cosine = Math.cos(radians);
    let left = Infinity;
    let right = -Infinity;
    let bottom = Infinity;
    let top = -Infinity;
    for (const [localX, localY] of LANDER_HULL) {
        const x = pose.x + localX * cosine + localY * sine;
        const y = pose.y - localX * sine + localY * cosine;
        left = Math.min(left, x);
        right = Math.max(right, x);
        bottom = Math.min(bottom, y);
        top = Math.max(top, y);
    }
    return { left, right, bottom, top };
}

function segmentDistanceSquared(a, b, c, d) {
    function orientation(p, q, r) {
        return (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x);
    }
    function onSegment(p, q, r) {
        return (
            q.x >= Math.min(p.x, r.x) &&
            q.x <= Math.max(p.x, r.x) &&
            q.y >= Math.min(p.y, r.y) &&
            q.y <= Math.max(p.y, r.y)
        );
    }
    const o1 = orientation(a, b, c);
    const o2 = orientation(a, b, d);
    const o3 = orientation(c, d, a);
    const o4 = orientation(c, d, b);
    if (
        (o1 === 0 && onSegment(a, c, b)) ||
        (o2 === 0 && onSegment(a, d, b)) ||
        (o3 === 0 && onSegment(c, a, d)) ||
        (o4 === 0 && onSegment(c, b, d)) ||
        (o1 > 0 !== o2 > 0 && o3 > 0 !== o4 > 0)
    )
        return 0;
    function pointDistanceSquared(point, start, end) {
        const dx = end.x - start.x;
        const dy = end.y - start.y;
        const lengthSquared = dx * dx + dy * dy;
        const projection =
            lengthSquared === 0
                ? 0
                : collisionClamp(((point.x - start.x) * dx + (point.y - start.y) * dy) / lengthSquared, 0, 1);
        const x = start.x + projection * dx;
        const y = start.y + projection * dy;
        return (point.x - x) ** 2 + (point.y - y) ** 2;
    }
    return Math.min(
        pointDistanceSquared(a, c, d),
        pointDistanceSquared(b, c, d),
        pointDistanceSquared(c, a, b),
        pointDistanceSquared(d, a, b),
    );
}

function polygonSegmentDistanceSquared(polygon, start, end) {
    let minimum = Infinity;
    for (let index = 0; index < polygon.length; index += 1) {
        minimum = Math.min(
            minimum,
            segmentDistanceSquared(polygon[index], polygon[(index + 1) % polygon.length], start, end),
        );
    }
    return minimum;
}

function rectangle(left, right, bottom, top) {
    return [
        { x: left, y: bottom },
        { x: right, y: bottom },
        { x: right, y: top },
        { x: left, y: top },
    ];
}

function pointInPolygon(point, polygon) {
    let inside = false;
    for (let index = 0, previous = polygon.length - 1; index < polygon.length; previous = index, index += 1) {
        const a = polygon[index];
        const b = polygon[previous];
        if (a.y > point.y !== b.y > point.y && point.x <= ((b.x - a.x) * (point.y - a.y)) / (b.y - a.y) + a.x)
            inside = !inside;
    }
    return inside;
}

function polygonDistanceSquared(left, right) {
    if (left.some((point) => pointInPolygon(point, right)) || right.some((point) => pointInPolygon(point, left)))
        return 0;
    let minimum = Infinity;
    for (let index = 0; index < right.length; index += 1) {
        minimum = Math.min(
            minimum,
            polygonSegmentDistanceSquared(left, right[index], right[(index + 1) % right.length]),
        );
    }
    return minimum;
}

function terrainSegments(model, bounds) {
    const vertices = model.terrainVertices;
    if (!vertices) throw new TypeError("Collision classification requires retained terrain vertices");
    const segments = [];
    const first = firstTerrainSegmentIndex(vertices, bounds.left - COLLISION_MARGIN);
    for (let index = first; index < vertices.length; index += 1) {
        const left = { x: vertices[index - 1][0], y: vertices[index - 1][1] };
        const right = { x: vertices[index][0], y: vertices[index][1] };
        if (left.x > bounds.right + COLLISION_MARGIN) break;
        segments.push([left, right]);
    }
    return segments;
}

function firstTerrainSegmentIndex(vertices, minimumRight) {
    let low = 1;
    let high = vertices.length;
    while (low < high) {
        const middle = Math.floor((low + high) / 2);
        if (vertices[middle][0] < minimumRight) low = middle + 1;
        else high = middle;
    }
    return low;
}

function terrainOverlaps(model, bounds) {
    const vertices = model.terrainVertices;
    if (!vertices) throw new TypeError("Collision classification requires retained terrain vertices");
    const first = firstTerrainSegmentIndex(vertices, bounds.left - COLLISION_MARGIN);
    for (let index = first; index < vertices.length; index += 1) {
        const left = vertices[index - 1];
        const right = vertices[index];
        if (left[0] > bounds.right + COLLISION_MARGIN) break;
        if (
            Math.max(left[1], right[1]) >= bounds.bottom - COLLISION_MARGIN &&
            Math.min(left[1], right[1]) <= bounds.top + COLLISION_MARGIN
        )
            return true;
    }
    return false;
}

function belowTerrain(hull, segment) {
    const [left, right] = segment;
    return hull.some((point) => {
        if (point.x < left.x || point.x > right.x) return false;
        const y = left.y + (right.y - left.y) * ((point.x - left.x) / (right.x - left.x));
        return point.y <= y;
    });
}

export function unsafeFeatures(model, pose, target, ignoredTopSiteId = null) {
    const features = [];
    for (const site of model.retainedSites) {
        const topLeft = { x: site.platformLeft, y: site.platformTop };
        const topRight = { x: site.platformRight, y: site.platformTop };
        const bottomLeft = { x: site.platformLeft, y: site.platformBottom };
        const bottomRight = { x: site.platformRight, y: site.platformBottom };
        if (site.id === target?.id || site.id === ignoredTopSiteId) {
            features.push({ cause: "platform", priority: 2, segment: [topLeft, bottomLeft] });
            features.push({ cause: "platform", priority: 2, segment: [bottomLeft, bottomRight] });
            features.push({ cause: "platform", priority: 2, segment: [bottomRight, topRight] });
        } else {
            features.push({
                cause: "platform",
                priority: 2,
                polygon: rectangle(site.platformLeft, site.platformRight, site.platformBottom, site.platformTop),
            });
        }
        const structure = siteStructure(site);
        features.push({
            cause: "truss",
            priority: 2,
            polygon: rectangle(
                structure.truss.left,
                structure.truss.right,
                structure.truss.bottom,
                structure.truss.top,
            ),
        });
        for (const column of structure.supportColumns) {
            features.push({
                cause: "column",
                priority: 2,
                polygon: rectangle(
                    column.collider.left,
                    column.collider.right,
                    column.collider.bottom,
                    column.collider.top,
                ),
            });
        }
        features.push({
            cause: "noc",
            priority: 1,
            polygon: rectangle(structure.noc.left, structure.noc.right, structure.noc.bottom, structure.noc.top),
        });
        features.push({
            cause: "mast",
            priority: 1,
            polygon: rectangle(structure.mast.left, structure.mast.right, structure.mast.bottom, structure.mast.top),
        });
    }
    return features.map((feature) => {
        const points = feature.polygon ?? feature.segment;
        return {
            ...feature,
            bounds: {
                left: Math.min(...points.map((point) => point.x)),
                right: Math.max(...points.map((point) => point.x)),
                bottom: Math.min(...points.map((point) => point.y)),
                top: Math.max(...points.map((point) => point.y)),
            },
        };
    });
}

function unsafeCauseAtPose(model, pose, target, ignoredTopSiteId = null, suppliedFeatures = null) {
    const hull = hullForPose(pose);
    const bounds = {
        left: Math.min(...hull.map((point) => point.x)),
        right: Math.max(...hull.map((point) => point.x)),
        bottom: Math.min(...hull.map((point) => point.y)),
        top: Math.max(...hull.map((point) => point.y)),
    };
    const marginSquared = COLLISION_MARGIN ** 2;
    const hits = [];
    for (const feature of suppliedFeatures ?? unsafeFeatures(model, pose, target, ignoredTopSiteId)) {
        const featureBounds = feature.bounds;
        if (
            featureBounds.right < bounds.left - COLLISION_MARGIN ||
            featureBounds.left > bounds.right + COLLISION_MARGIN ||
            featureBounds.top < bounds.bottom - COLLISION_MARGIN ||
            featureBounds.bottom > bounds.top + COLLISION_MARGIN
        )
            continue;
        const distance = feature.polygon
            ? polygonDistanceSquared(hull, feature.polygon)
            : polygonSegmentDistanceSquared(hull, feature.segment[0], feature.segment[1]);
        if (distance <= marginSquared) hits.push(feature);
    }
    for (const segment of terrainSegments(model, bounds)) {
        if (
            belowTerrain(hull, segment) ||
            polygonSegmentDistanceSquared(hull, segment[0], segment[1]) <= marginSquared
        ) {
            hits.push({ cause: "terrain", priority: 3 });
        }
    }
    hits.sort((left, right) => left.priority - right.priority);
    return hits[0]?.cause ?? null;
}

function targetTopSweptContact(previous, next, target, initialLeftBounds, initialRightBounds) {
    const topLeft = { x: target.platformLeft, y: target.platformTop };
    const topRight = { x: target.platformRight, y: target.platformTop };
    function search(leftPose, rightPose, leftBounds, rightBounds, leftTime, rightTime, depth) {
        const translation = Math.hypot(rightPose.x - leftPose.x, rightPose.y - leftPose.y);
        const rotation = HULL_RADIUS * Math.abs((normalizeDegrees(rightPose.angle - leftPose.angle) * Math.PI) / 180);
        const bound = translation + rotation;
        const enclosure = {
            left: Math.min(leftBounds.left, rightBounds.left) - bound,
            right: Math.max(leftBounds.right, rightBounds.right) + bound,
            bottom: Math.min(leftBounds.bottom, rightBounds.bottom) - bound,
            top: Math.max(leftBounds.top, rightBounds.top) + bound,
        };
        if (
            enclosure.right < target.platformLeft ||
            enclosure.left > target.platformRight ||
            enclosure.bottom > target.platformTop ||
            enclosure.top < target.platformTop
        )
            return null;
        if (leftBounds.bottom > target.platformTop && rightBounds.bottom <= target.platformTop) {
            let clear = leftPose;
            let hit = rightPose;
            let clearTime = leftTime;
            let hitTime = rightTime;
            for (let iteration = 0; iteration < 12; iteration += 1) {
                const middleTime = (clearTime + hitTime) / 2;
                const middle = interpolatePose(previous, next, middleTime);
                if (hullBounds(middle).bottom <= target.platformTop) {
                    hit = middle;
                    hitTime = middleTime;
                } else {
                    clear = middle;
                    clearTime = middleTime;
                }
            }
            void clear;
            if (polygonSegmentDistanceSquared(hullForPose(hit), topLeft, topRight) <= Number.EPSILON) {
                return { pose: hit, time: hitTime, grazing: false };
            }
        }
        if (depth >= 20 || (rightTime - leftTime) * COLLISION_STEP_SECONDS <= 1e-7) {
            return {
                pose: interpolatePose(previous, next, (leftTime + rightTime) / 2),
                time: (leftTime + rightTime) / 2,
                grazing: true,
            };
        }
        const middleTime = (leftTime + rightTime) / 2;
        const middle = interpolatePose(previous, next, middleTime);
        const middleBounds = hullBounds(middle);
        return (
            search(leftPose, middle, leftBounds, middleBounds, leftTime, middleTime, depth + 1) ??
            search(middle, rightPose, middleBounds, rightBounds, middleTime, rightTime, depth + 1)
        );
    }
    return search(previous, next, initialLeftBounds, initialRightBounds, 0, 1, 0);
}

export function classifySweptContactFromBounds(model, previous, next, previousBounds, nextBounds, options) {
    const target = collisionSiteById(model, model.targetSiteId);
    const travel =
        Math.hypot(next.x - previous.x, next.y - previous.y) +
        HULL_RADIUS * Math.abs((normalizeDegrees(next.angle - previous.angle) * Math.PI) / 180);
    const intervals = Math.max(1, Math.ceil(travel / COLLISION_MARGIN));
    if (intervals > 64) return { kind: "unsafe", cause: "overspeed", pose: next };
    const swept = {
        left: Math.min(previousBounds.left, nextBounds.left) - travel,
        right: Math.max(previousBounds.right, nextBounds.right) + travel,
        bottom: Math.min(previousBounds.bottom, nextBounds.bottom) - travel,
        top: Math.max(previousBounds.top, nextBounds.top) + travel,
    };
    const overlaps = (bounds) =>
        bounds.right >= swept.left - COLLISION_MARGIN &&
        bounds.left <= swept.right + COLLISION_MARGIN &&
        bounds.top >= swept.bottom - COLLISION_MARGIN &&
        bounds.bottom <= swept.top + COLLISION_MARGIN;
    const features = options.features ?? unsafeFeatures(model, previous, target, options.ignoreTopSiteId);
    const topPossible =
        target &&
        target.platformRight >= swept.left &&
        target.platformLeft <= swept.right &&
        target.platformTop >= swept.bottom &&
        target.platformTop <= swept.top;
    const featurePossible = features.some((feature) => overlaps(feature.bounds));
    const terrainPossible = terrainOverlaps(model, swept);
    if (!topPossible && !featurePossible && !terrainPossible) return null;
    const topContact = topPossible ? targetTopSweptContact(previous, next, target, previousBounds, nextBounds) : null;
    let unsafeContact = null;
    let clearPose = previous;
    let clearTime = 0;
    for (let index = 0; index <= intervals; index += 1) {
        const time = index / intervals;
        const pose = interpolatePose(previous, next, time);
        const cause = unsafeCauseAtPose(model, pose, target, options.ignoreTopSiteId, features);
        if (cause) {
            let hitPose = pose;
            let hitTime = time;
            let hitCause = cause;
            for (let iteration = 0; index > 0 && iteration < 12; iteration += 1) {
                const middleTime = (clearTime + hitTime) / 2;
                const middle = interpolatePose(previous, next, middleTime);
                const middleCause = unsafeCauseAtPose(model, middle, target, options.ignoreTopSiteId, features);
                if (middleCause) {
                    hitPose = middle;
                    hitTime = middleTime;
                    hitCause = middleCause;
                } else {
                    clearPose = middle;
                    clearTime = middleTime;
                }
            }
            void clearPose;
            unsafeContact = { kind: "unsafe", cause: hitCause, pose: hitPose, time: hitTime };
            break;
        }
        clearPose = pose;
        clearTime = time;
    }
    if (unsafeContact && (!topContact || unsafeContact.time <= topContact.time + 1e-12)) return unsafeContact;
    if (topContact) {
        const pose = topContact.pose;
        const feet = [transformLocalPoint(pose, -1.6, 0), transformLocalPoint(pose, 1.6, 0)];
        const safe =
            !topContact.grazing &&
            pose.vy <= 0 &&
            feet.every((foot) => foot.x >= target.platformLeft && foot.x <= target.platformRight) &&
            Math.abs(pose.vx) <= MAX_LANDING_HORIZONTAL_SPEED &&
            Math.abs(pose.vy) <= MAX_LANDING_DESCENT_SPEED &&
            Math.abs(normalizeDegrees(pose.angle)) <= MAX_LANDING_ANGLE &&
            Math.abs(pose.angularVelocity) <= MAX_LANDING_ANGULAR_SPEED;
        return {
            kind: safe ? "safe" : "unsafe",
            cause: safe ? "target" : topContact.grazing ? "grazing" : "target-envelope",
            pose,
            time: topContact.time,
        };
    }
    return null;
}

export function classifySweptContact(model, previous, next, options = {}) {
    return classifySweptContactFromBounds(model, previous, next, hullBounds(previous), hullBounds(next), options);
}
