const EPSILON = 1e-9;

function clean(value) {
    return Number(value.toFixed(12));
}

function pointKey(point) {
    return `${clean(point[0])},${clean(point[1])}`;
}

function cross(left, right) {
    return left[0] * right[1] - left[1] * right[0];
}

function subtract(left, right) {
    return [left[0] - right[0], left[1] - right[1]];
}

function parameterOnSegment(point, segment) {
    const delta = subtract(segment.end, segment.start);
    const lengthSquared = delta[0] ** 2 + delta[1] ** 2;
    if (lengthSquared <= EPSILON) throw new Error("Clear-face overlay contains a zero-length segment");
    return ((point[0] - segment.start[0]) * delta[0] + (point[1] - segment.start[1]) * delta[1]) /
        lengthSquared;
}

function pointOnSegment(point, segment) {
    const delta = subtract(segment.end, segment.start);
    const offset = subtract(point, segment.start);
    const parameter = parameterOnSegment(point, segment);
    return Math.abs(cross(delta, offset)) <= EPSILON && parameter >= -EPSILON && parameter <= 1 + EPSILON;
}

function intersections(left, right) {
    const leftDelta = subtract(left.end, left.start);
    const rightDelta = subtract(right.end, right.start);
    const offset = subtract(right.start, left.start);
    const denominator = cross(leftDelta, rightDelta);
    if (Math.abs(denominator) <= EPSILON) {
        if (Math.abs(cross(offset, leftDelta)) > EPSILON) return [];
        return [left.start, left.end, right.start, right.end]
            .filter((point) => pointOnSegment(point, left) && pointOnSegment(point, right));
    }
    const leftParameter = cross(offset, rightDelta) / denominator;
    const rightParameter = cross(offset, leftDelta) / denominator;
    if (leftParameter < -EPSILON || leftParameter > 1 + EPSILON ||
        rightParameter < -EPSILON || rightParameter > 1 + EPSILON) return [];
    return [[
        left.start[0] + leftDelta[0] * leftParameter,
        left.start[1] + leftDelta[1] * leftParameter,
    ]];
}

function splitOverlay(segments) {
    const splitPoints = segments.map((segment) => [segment.start, segment.end]);
    for (let left = 0; left < segments.length; left += 1) {
        for (let right = left + 1; right < segments.length; right += 1) {
            const shared = intersections(segments[left], segments[right]);
            splitPoints[left].push(...shared);
            splitPoints[right].push(...shared);
        }
    }
    const points = new Map();
    const edges = new Map();
    const addPoint = (point) => {
        const normalized = [clean(point[0]), clean(point[1])];
        const key = pointKey(normalized);
        points.set(key, normalized);
        return key;
    };
    for (let index = 0; index < segments.length; index += 1) {
        const segment = segments[index];
        const ordered = [...new Map(splitPoints[index]
            .map((point) => [pointKey(point), point])).values()]
            .sort((left, right) => parameterOnSegment(left, segment) - parameterOnSegment(right, segment));
        for (let pointIndex = 1; pointIndex < ordered.length; pointIndex += 1) {
            const leftKey = addPoint(ordered[pointIndex - 1]);
            const rightKey = addPoint(ordered[pointIndex]);
            if (leftKey === rightKey) continue;
            const key = [leftKey, rightKey].sort().join("|");
            edges.set(key, [leftKey, rightKey]);
        }
    }
    return { edges: [...edges.values()], points };
}

function boundedFaces(overlay) {
    const neighbors = new Map([...overlay.points.keys()].map((key) => [key, new Set()]));
    for (const [left, right] of overlay.edges) {
        neighbors.get(left).add(right);
        neighbors.get(right).add(left);
    }
    const orderedNeighbors = new Map([...neighbors].map(([key, adjacent]) => {
        const origin = overlay.points.get(key);
        return [key, [...adjacent].sort((left, right) => {
            const leftPoint = overlay.points.get(left);
            const rightPoint = overlay.points.get(right);
            return Math.atan2(leftPoint[1] - origin[1], leftPoint[0] - origin[0]) -
                Math.atan2(rightPoint[1] - origin[1], rightPoint[0] - origin[0]);
        })];
    }));
    const visited = new Set();
    const faces = [];
    for (const [startLeft, startRight] of overlay.edges.flatMap(([left, right]) => [[left, right], [right, left]])) {
        if (visited.has(`${startLeft}>${startRight}`)) continue;
        const keys = [];
        let left = startLeft;
        let right = startRight;
        while (!visited.has(`${left}>${right}`)) {
            visited.add(`${left}>${right}`);
            keys.push(left);
            const adjacent = orderedNeighbors.get(right);
            const reverseIndex = adjacent.indexOf(left);
            if (reverseIndex < 0) throw new Error("Clear-face overlay contains a disconnected half-edge");
            const next = adjacent[(reverseIndex - 1 + adjacent.length) % adjacent.length];
            left = right;
            right = next;
        }
        if (left !== startLeft || right !== startRight || keys.length < 3) continue;
        const polygon = keys.map((key) => overlay.points.get(key));
        const area = polygon.reduce((total, point, index) => {
            const next = polygon[(index + 1) % polygon.length];
            return total + point[0] * next[1] - next[0] * point[1];
        }, 0) / 2;
        if (area > EPSILON) faces.push(polygon);
    }
    return faces;
}

function pointInCollider(point, collider) {
    return point[0] >= collider.left - EPSILON && point[0] <= collider.right + EPSILON &&
        point[1] >= collider.bottom - EPSILON && point[1] <= collider.top + EPSILON;
}

function strokeFitsCollider(member, collider, halfWidth) {
    const xs = [member.start[0], member.end[0]];
    const ys = [member.start[1], member.end[1]];
    return Math.min(...xs) - halfWidth >= collider.left - EPSILON &&
        Math.max(...xs) + halfWidth <= collider.right + EPSILON &&
        Math.min(...ys) - halfWidth >= collider.bottom - EPSILON &&
        Math.max(...ys) + halfWidth <= collider.top + EPSILON;
}

export function enumerateConnectedClearFaces({ colliders, members, terrainSegments, memberWidth }) {
    if (!Number.isFinite(memberWidth) || memberWidth <= 0) throw new TypeError("Member width must be positive");
    if (!Array.isArray(colliders) || colliders.length === 0) {
        throw new TypeError("Clear-face overlay requires collider bounds");
    }
    const halfWidth = memberWidth / 2;
    for (const member of members) {
        if (member.cap !== "butt" || member.join !== "round") {
            throw new Error("Clear-face overlay requires butt-capped, round-joined members");
        }
        if (!colliders.some((collider) => strokeFitsCollider(member, collider, halfWidth))) {
            throw new Error("A stroked member escapes the reviewed collider union");
        }
    }
    const axes = members.concat(terrainSegments).map((segment) => ({
        start: [...segment.start], end: [...segment.end],
    }));
    return boundedFaces(splitOverlay(axes)).filter((polygon) =>
        polygon.every((point) => colliders.some((collider) => pointInCollider(point, collider)))).map((polygon) => {
        const xs = polygon.map(([x]) => x);
        const ys = polygon.map(([, y]) => y);
        const width = clean(Math.max(...xs) - Math.min(...xs));
        const height = clean(Math.max(...ys) - Math.min(...ys));
        return { diameter: Math.hypot(width, height), height, width };
    });
}

export function maximumConnectedClearFace(input) {
    const faces = enumerateConnectedClearFaces(input);
    if (faces.length === 0) throw new Error("Clear-face overlay produced no bounded faces");
    return faces.reduce((maximum, face) => face.diameter > maximum.diameter ? face : maximum);
}
