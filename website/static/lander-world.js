export const STATIC_WORLD_SEED = 0x41475731;
export const CHUNK_WIDTH = 50;
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

export const TERRAIN_SUBDIVISIONS = 8;
export const TERRAIN_GRADE_LIMIT = 0.32407407407407407;
export const TERRAIN_GRADE_CHANGE_LIMIT = 0.15707517611697638;

const TERRAIN_FAMILIES = freeze([
    {
        id: "A",
        blockWidth: 261,
        slots: [
            { terrainLevel: 59, templateId: "route-81-rise", centerDelta: 81 },
            { terrainLevel: 75, templateId: "route-84-fall", centerDelta: 84 },
            { terrainLevel: 67, templateId: "route-96-fall", centerDelta: 96 },
        ],
    },
    {
        id: "B",
        blockWidth: 276,
        slots: [
            { terrainLevel: 59, templateId: "route-87-rise", centerDelta: 87 },
            { terrainLevel: 67, templateId: "route-99-rise", centerDelta: 99 },
            { terrainLevel: 75, templateId: "route-90-fall", centerDelta: 90 },
        ],
    },
]);

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

export function terrainCycleForSeed(seed) {
    const family = TERRAIN_FAMILIES[sampleUnit(seed, 13, 0) < 0.5 ? 0 : 1];
    const phase = Math.floor(3 * sampleUnit(seed, 13, 1));
    const slots = Array.from({ length: 3 }, (_, index) => family.slots[(phase + index) % 3]);
    return freeze({ family: family.id, phase, blockWidth: family.blockWidth, slots });
}

export function terrainSiteForIndex(seed, siteIndex) {
    if (!Number.isSafeInteger(siteIndex)) throw new TypeError("Site index must be a safe integer");
    const cycle = terrainCycleForSeed(seed);
    const block = Math.floor(siteIndex / 3);
    const slotIndex = positiveModulo(siteIndex, 3);
    let center = 36 + block * cycle.blockWidth;
    for (let index = 0; index < slotIndex; index += 1) center += cycle.slots[index].centerDelta;
    const slot = cycle.slots[slotIndex];
    return freeze({
        index: siteIndex,
        center,
        terrainLevel: slot.terrainLevel,
        deckLevel: slot.terrainLevel + 24,
        templateId: slot.templateId,
        centerDelta: slot.centerDelta,
        valleyLevel: 5 + Math.floor(16 * sampleUnit(seed, 14, siteIndex >>> 0)),
        family: cycle.family,
        phase: cycle.phase,
        blockWidth: cycle.blockWidth,
    });
}

function terrainLegForX(seed, x) {
    const cycle = terrainCycleForSeed(seed);
    let siteIndex = Math.floor((x - 36) / cycle.blockWidth) * 3;
    let left = terrainSiteForIndex(seed, siteIndex);
    for (let offset = 0; offset < 3; offset += 1) {
        const right = terrainSiteForIndex(seed, siteIndex + 1);
        if (x <= right.center) return { left, right };
        siteIndex += 1;
        left = right;
    }
    throw new Error(`Terrain leg lookup failed at ${x}`);
}

function smootherstep(value) {
    return 6 * value ** 5 - 15 * value ** 4 + 10 * value ** 3;
}

function designTerrainHeight(seed, x) {
    const { left, right } = terrainLegForX(seed, x);
    const middle = (left.center + right.center) / 2;
    const valley = left.valleyLevel / 10;
    if (x <= middle) {
        const ratio = (x - left.center) / (middle - left.center);
        return left.terrainLevel / 10 + (valley - left.terrainLevel / 10) * smootherstep(ratio);
    }
    const ratio = (x - middle) / (right.center - middle);
    return valley + (right.terrainLevel / 10 - valley) * smootherstep(ratio);
}

export function terrainHeightAt(seed, x) {
    if (!Number.isFinite(x)) throw new TypeError("Terrain X must be finite");
    const { left, right } = terrainLegForX(seed, x);
    const middle = (left.center + right.center) / 2;
    const start = x <= middle ? left.center : middle;
    const end = x <= middle ? middle : right.center;
    const scaled = ((x - start) / (end - start)) * TERRAIN_SUBDIVISIONS;
    const segment = Math.min(TERRAIN_SUBDIVISIONS - 1, Math.max(0, Math.floor(scaled)));
    const segmentLeft = start + (end - start) * segment / TERRAIN_SUBDIVISIONS;
    const segmentRight = start + (end - start) * (segment + 1) / TERRAIN_SUBDIVISIONS;
    const leftHeight = designTerrainHeight(seed, segmentLeft);
    const rightHeight = designTerrainHeight(seed, segmentRight);
    return leftHeight + (rightHeight - leftHeight) * ((x - segmentLeft) / (segmentRight - segmentLeft));
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

export function createSiteForIndex(seed, siteIndex, state = {}) {
    const normalized = normalizeSeed(seed);
    const terrainSite = terrainSiteForIndex(normalized, siteIndex);
    const center = terrainSite.center;
    const platformLeft = center - PLATFORM_WIDTH / 2;
    const platformRight = center + PLATFORM_WIDTH / 2;
    const deckLevel = terrainSite.deckLevel;
    const platformTop = deckLevel / 10;
    return freeze({
        id: siteIndex,
        seed: normalized,
        center,
        deckLevel,
        terrainLevel: terrainSite.terrainLevel,
        platformLeft,
        platformRight,
        platformTop,
        platformBottom: platformTop - PLATFORM_THICKNESS,
        canCollected: state.canCollected ?? false,
        powered: state.powered ?? false,
        nocStage: state.nocStage ?? 0,
        templateId: state.templateId ?? (siteIndex === 0 ? "initial" : null),
        originSiteId: state.originSiteId ?? null,
        clearanceKnots: state.clearanceKnots?.map((point) => [...point]) ?? null,
    });
}

export function createFirstSite(seed) {
    return createSiteForIndex(seed, 0);
}

export function selectTemplate(seed, siteIndex, originSite, templates) {
    const origin = terrainSiteForIndex(seed, siteIndex - 1);
    const target = terrainSiteForIndex(seed, siteIndex);
    if (originSite.id !== siteIndex - 1 || originSite.center !== origin.center ||
        originSite.deckLevel !== origin.deckLevel || target.center - origin.center !== origin.centerDelta) {
        throw new Error(`Site ${siteIndex - 1} does not match its direct terrain cycle`);
    }
    const template = templates.find((candidate) => candidate.templateId === origin.templateId);
    const deckDelta = (target.deckLevel - origin.deckLevel) / 10;
    if (!template || template.centerDelta !== origin.centerDelta || template.deckDelta !== deckDelta) {
        throw new Error(`Missing exact route template ${origin.templateId}`);
    }
    return template;
}

export function instantiateTemplateSite(seed, siteIndex, originSite, template) {
    const origin = terrainSiteForIndex(seed, siteIndex - 1);
    const target = terrainSiteForIndex(seed, siteIndex);
    if (originSite.id !== siteIndex - 1 || originSite.center !== origin.center ||
        originSite.deckLevel !== origin.deckLevel || template.templateId !== origin.templateId ||
        template.centerDelta !== target.center - origin.center ||
        template.deckDelta !== (target.deckLevel - origin.deckLevel) / 10) {
        throw new RangeError(`Route template ${template.templateId} does not match the direct terrain cycle`);
    }
    return createSiteForIndex(seed, siteIndex, {
        templateId: template.templateId,
        originSiteId: originSite.id,
        clearanceKnots: template.clearanceKnots,
    });
}

export function terrainVerticesForWindow(seed, sites, left, right) {
    if (!Number.isFinite(left) || !Number.isFinite(right) || right < left) {
        throw new RangeError("Terrain range must be finite and ordered");
    }
    const points = new Set([left, right]);
    let siteIndex = terrainLegForX(seed, left).left.index;
    while (terrainSiteForIndex(seed, siteIndex).center < right) {
        const origin = terrainSiteForIndex(seed, siteIndex);
        const target = terrainSiteForIndex(seed, siteIndex + 1);
        const middle = (origin.center + target.center) / 2;
        for (const [start, end] of [[origin.center, middle], [middle, target.center]]) {
            for (let step = 0; step <= TERRAIN_SUBDIVISIONS; step += 1) {
                const x = start + (end - start) * step / TERRAIN_SUBDIVISIONS;
                if (x >= left && x <= right) points.add(x);
            }
        }
        siteIndex += 1;
    }
    for (const site of sites) {
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
    if (vertices.length > 72) throw new Error(`Terrain projection exceeds 72 vertices: ${vertices.length}`);
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
