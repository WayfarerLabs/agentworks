const TOLERANCE = 1e-9;

const snap = (value) => Number(value.toFixed(12));
const keyFor = ([x, y]) => `${snap(x)},${snap(y)}`;
const vector = (from, to) => [to[0] - from[0], to[1] - from[1]];
const cross = ([ax, ay], [bx, by]) => ax * by - ay * bx;

function parameter(segment, point) {
    const direction = vector(segment.start, segment.end);
    const offset = vector(segment.start, point);
    const squaredLength = direction[0] ** 2 + direction[1] ** 2;
    return (offset[0] * direction[0] + offset[1] * direction[1]) / squaredLength;
}

function liesOn(segment, point) {
    const along = parameter(segment, point);
    return Math.abs(cross(vector(segment.start, segment.end), vector(segment.start, point))) <= TOLERANCE &&
        along >= -TOLERANCE && along <= 1 + TOLERANCE;
}

function crossings(first, second) {
    const firstDirection = vector(first.start, first.end);
    const secondDirection = vector(second.start, second.end);
    const between = vector(first.start, second.start);
    const divisor = cross(firstDirection, secondDirection);
    if (Math.abs(divisor) <= TOLERANCE) {
        if (Math.abs(cross(between, firstDirection)) > TOLERANCE) return [];
        return [first.start, first.end, second.start, second.end]
            .filter((point) => liesOn(first, point) && liesOn(second, point));
    }
    const firstParameter = cross(between, secondDirection) / divisor;
    const secondParameter = cross(between, firstDirection) / divisor;
    if (firstParameter < -TOLERANCE || firstParameter > 1 + TOLERANCE ||
        secondParameter < -TOLERANCE || secondParameter > 1 + TOLERANCE) return [];
    return [[first.start[0] + firstDirection[0] * firstParameter,
        first.start[1] + firstDirection[1] * firstParameter]];
}

function planarGraph(segments) {
    const cuts = segments.map((segment) => [segment.start, segment.end]);
    segments.forEach((segment, first) => segments.slice(first + 1).forEach((other, offset) => {
        const second = first + offset + 1;
        const shared = crossings(segment, other);
        cuts[first].push(...shared);
        cuts[second].push(...shared);
    }));
    const points = new Map();
    const edges = new Map();
    segments.forEach((segment, index) => {
        const ordered = [...new Map(cuts[index].map((point) => [keyFor(point), point])).values()]
            .sort((left, right) => parameter(segment, left) - parameter(segment, right));
        ordered.slice(1).forEach((right, pointIndex) => {
            const left = ordered[pointIndex];
            const leftKey = keyFor(left); const rightKey = keyFor(right);
            if (leftKey === rightKey) return;
            points.set(leftKey, left.map(snap)); points.set(rightKey, right.map(snap));
            edges.set([leftKey, rightKey].sort().join("|"), [leftKey, rightKey]);
        });
    });
    return { edges: [...edges.values()], points };
}

function enclosedPolygons(graph) {
    const neighbors = new Map([...graph.points.keys()].map((key) => [key, []]));
    graph.edges.forEach(([left, right]) => {
        neighbors.get(left).push(right); neighbors.get(right).push(left);
    });
    neighbors.forEach((adjacent, originKey) => {
        const origin = graph.points.get(originKey);
        adjacent.sort((left, right) => {
            const leftPoint = graph.points.get(left); const rightPoint = graph.points.get(right);
            return Math.atan2(leftPoint[1] - origin[1], leftPoint[0] - origin[0]) -
                Math.atan2(rightPoint[1] - origin[1], rightPoint[0] - origin[0]);
        });
    });
    const visited = new Set(); const polygons = [];
    graph.edges.flatMap(([left, right]) => [[left, right], [right, left]]).forEach(([start, nextStart]) => {
        if (visited.has(`${start}>${nextStart}`)) return;
        let previous = start; let current = nextStart; const polygon = [];
        while (!visited.has(`${previous}>${current}`)) {
            visited.add(`${previous}>${current}`); polygon.push(graph.points.get(previous));
            const adjacent = neighbors.get(current); const reverse = adjacent.indexOf(previous);
            [previous, current] = [current, adjacent[(reverse - 1 + adjacent.length) % adjacent.length]];
        }
        if (previous !== start || current !== nextStart || polygon.length < 3) return;
        const twiceArea = polygon.reduce((area, point, index) => {
            const next = polygon[(index + 1) % polygon.length];
            return area + point[0] * next[1] - next[0] * point[1];
        }, 0);
        if (twiceArea > TOLERANCE) polygons.push(polygon);
    });
    return polygons;
}

function insideAnyCollider(point, colliders) {
    return colliders.some((collider) => point[0] >= collider.left - TOLERANCE &&
        point[0] <= collider.right + TOLERANCE && point[1] >= collider.bottom - TOLERANCE &&
        point[1] <= collider.top + TOLERANCE);
}

export function independentMaximumClearFace(site, members = site.scaffoldMembers) {
    const terrain = site.supportColumns.map((column) => ({
        start: [column.left, column.leftFoot], end: [column.right, column.rightFoot],
    }));
    const colliders = [site.truss, ...site.supportColumns.map((column) => column.collider)];
    const faces = enclosedPolygons(planarGraph([...members, ...terrain])).filter((polygon) =>
        polygon.every((point) => insideAnyCollider(point, colliders))).map((polygon) => {
        const xs = polygon.map(([x]) => x); const ys = polygon.map(([, y]) => y);
        const width = snap(Math.max(...xs) - Math.min(...xs));
        const height = snap(Math.max(...ys) - Math.min(...ys));
        return { diameter: Math.hypot(width, height), height, width };
    });
    return faces.reduce((maximum, face) => face.diameter > maximum.diameter ? face : maximum);
}
