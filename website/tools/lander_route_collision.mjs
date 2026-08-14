const MARGIN = 0.02;

const clamp = (value, minimum, maximum) => Math.min(maximum, Math.max(minimum, value));
const normalizeDegrees = (value) => ((((value + 180) % 360) + 360) % 360) - 180;

function transform(pose, x, y) {
    const radians = (pose.angle * Math.PI) / 180;
    return {
        x: pose.x + x * Math.cos(radians) + y * Math.sin(radians),
        y: pose.y - x * Math.sin(radians) + y * Math.cos(radians),
    };
}

export const routeHull = (pose) => [
    transform(pose, -1.6, 0),
    transform(pose, 1.6, 0),
    transform(pose, 1.6, 6.5),
    transform(pose, -1.6, 6.5),
];

const mixHull = (left, right, amount) =>
    left.map((point, index) => ({
        x: point.x + (right[index].x - point.x) * amount,
        y: point.y + (right[index].y - point.y) * amount,
    }));

function bounds(...groups) {
    const points = groups.flat();
    return {
        left: Math.min(...points.map(({ x }) => x)),
        right: Math.max(...points.map(({ x }) => x)),
        bottom: Math.min(...points.map(({ y }) => y)),
        top: Math.max(...points.map(({ y }) => y)),
    };
}

const candidateBounds = (candidate) => candidate.bounds ?? bounds(candidate.polygon ?? candidate.segment);
const overlaps = (left, right, margin = 0) =>
    left.right >= right.left - margin &&
    left.left <= right.right + margin &&
    left.top >= right.bottom - margin &&
    left.bottom <= right.top + margin;

function segmentDistanceSquared(a, b, c, d) {
    const orientation = (p, q, r) => (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x);
    const on = (p, q, r) =>
        q.x >= Math.min(p.x, r.x) &&
        q.x <= Math.max(p.x, r.x) &&
        q.y >= Math.min(p.y, r.y) &&
        q.y <= Math.max(p.y, r.y);
    const values = [orientation(a, b, c), orientation(a, b, d), orientation(c, d, a), orientation(c, d, b)];
    if (
        (values[0] === 0 && on(a, c, b)) ||
        (values[1] === 0 && on(a, d, b)) ||
        (values[2] === 0 && on(c, a, d)) ||
        (values[3] === 0 && on(c, b, d)) ||
        (values[0] > 0 !== values[1] > 0 && values[2] > 0 !== values[3] > 0)
    )
        return 0;
    const pointDistance = (point, start, end) => {
        const dx = end.x - start.x,
            dy = end.y - start.y,
            length = dx * dx + dy * dy,
            amount = length ? clamp(((point.x - start.x) * dx + (point.y - start.y) * dy) / length, 0, 1) : 0,
            x = start.x + amount * dx,
            y = start.y + amount * dy;
        return (point.x - x) ** 2 + (point.y - y) ** 2;
    };
    return Math.min(pointDistance(a, c, d), pointDistance(b, c, d), pointDistance(c, a, b), pointDistance(d, a, b));
}

function polygonSegmentDistanceSquared(polygon, start, end) {
    let minimum = Infinity;
    for (let index = 0; index < polygon.length; index += 1)
        minimum = Math.min(
            minimum,
            segmentDistanceSquared(polygon[index], polygon[(index + 1) % polygon.length], start, end),
        );
    return minimum;
}

function pointInPolygon(point, polygon) {
    let inside = false;
    for (let index = 0, previous = polygon.length - 1; index < polygon.length; previous = index, index += 1) {
        const a = polygon[index],
            b = polygon[previous];
        if (a.y > point.y !== b.y > point.y && point.x <= ((b.x - a.x) * (point.y - a.y)) / (b.y - a.y) + a.x)
            inside = !inside;
    }
    return inside;
}

function polygonDistanceSquared(left, right) {
    if (left.some((point) => pointInPolygon(point, right)) || right.some((point) => pointInPolygon(point, left)))
        return 0;
    let minimum = Infinity;
    for (let index = 0; index < right.length; index += 1)
        minimum = Math.min(
            minimum,
            polygonSegmentDistanceSquared(left, right[index], right[(index + 1) % right.length]),
        );
    return minimum;
}

function candidateDistanceSquared(hull, candidate) {
    if (candidate.solidBelow) {
        const [left, right] = candidate.segment;
        if (
            hull.some((point) => {
                if (point.x < left.x || point.x > right.x) return false;
                const y = left.y + ((right.y - left.y) * (point.x - left.x)) / (right.x - left.x);
                return point.y <= y;
            })
        )
            return 0;
    }
    return candidate.polygon
        ? polygonDistanceSquared(hull, candidate.polygon)
        : polygonSegmentDistanceSquared(hull, candidate.segment[0], candidate.segment[1]);
}

const DOUBLE_VIEW = new DataView(new ArrayBuffer(8));
function dyadicInteger(value) {
    if (value === 0) return { exponent: 0, integer: 0n };
    DOUBLE_VIEW.setFloat64(0, value, false);
    const high = DOUBLE_VIEW.getUint32(0, false),
        low = DOUBLE_VIEW.getUint32(4, false),
        sign = high >>> 31 ? -1n : 1n,
        exponentBits = (high >>> 20) & 0x7ff,
        fraction = (BigInt(high & 0xfffff) << 32n) | BigInt(low);
    return exponentBits === 0
        ? { exponent: -1074, integer: sign * fraction }
        : { exponent: exponentBits - 1075, integer: sign * ((1n << 52n) | fraction) };
}

const absBigInt = (value) => (value < 0n ? -value : value);
function gcd(left, right) {
    left = absBigInt(left);
    right = absBigInt(right);
    while (right) [left, right] = [right, left % right];
    return left;
}
function rational(numerator, denominator = 1n) {
    if (denominator < 0n) {
        numerator = -numerator;
        denominator = -denominator;
    }
    const divisor = gcd(numerator, denominator) || 1n;
    return { numerator: numerator / divisor, denominator: denominator / divisor };
}
const ZERO_RATIONAL = rational(0n);
const ONE_RATIONAL = rational(1n);
const compareRationals = (left, right) =>
    left.numerator * right.denominator < right.numerator * left.denominator
        ? -1
        : left.numerator * right.denominator > right.numerator * left.denominator
          ? 1
          : 0;
const midpoint = (left, right) =>
    rational(
        left.numerator * right.denominator + right.numerator * left.denominator,
        2n * left.denominator * right.denominator,
    );

function integerPolynomial(coefficients) {
    const parts = coefficients.map(dyadicInteger),
        exponent = Math.min(...parts.map((part) => part.exponent)),
        result = parts.map((part) => part.integer << BigInt(part.exponent - exponent));
    while (result.length > 1 && result.at(-1) === 0n) result.pop();
    const divisor = result.reduce((value, coefficient) => gcd(value, coefficient), 0n) || 1n;
    const sign = result.at(-1) < 0n ? -1n : 1n;
    return result.map((coefficient) => (coefficient / divisor) * sign);
}
function polynomialSignAt(polynomial, value) {
    const [constant = 0n, linear = 0n, quadratic = 0n] = polynomial,
        numerator = value.numerator,
        denominator = value.denominator,
        result =
            constant * denominator * denominator + linear * numerator * denominator + quadratic * numerator * numerator;
    return result < 0n ? -1 : result > 0n ? 1 : 0;
}
function integerSquareRoot(value) {
    if (value < 2n) return value;
    let left = 1n,
        right = 1n << BigInt(Math.ceil(value.toString(2).length / 2));
    while (left < right) {
        const middle = (left + right + 1n) >> 1n;
        if (middle * middle <= value) left = middle;
        else right = middle - 1n;
    }
    return left;
}
function rootFromRational(value) {
    return { exact: value, left: value, right: value, polynomial: [value.numerator, -value.denominator], ordinal: 0 };
}
function rootsInUnit(coefficients) {
    const polynomial = integerPolynomial(coefficients),
        [constant = 0n, linear = 0n, quadratic = 0n] = polynomial;
    if (quadratic === 0n) {
        if (linear === 0n) return [];
        const value = rational(-constant, linear);
        return compareRationals(value, ZERO_RATIONAL) >= 0 && compareRationals(value, ONE_RATIONAL) <= 0
            ? [rootFromRational(value)]
            : [];
    }
    const discriminant = linear * linear - 4n * quadratic * constant;
    if (discriminant < 0n) return [];
    const squareRoot = integerSquareRoot(discriminant);
    if (squareRoot * squareRoot === discriminant) {
        const roots = [rational(-linear - squareRoot, 2n * quadratic)];
        if (squareRoot !== 0n) roots.push(rational(-linear + squareRoot, 2n * quadratic));
        return roots
            .filter((root) => compareRationals(root, ZERO_RATIONAL) >= 0 && compareRationals(root, ONE_RATIONAL) <= 0)
            .sort(compareRationals)
            .map(rootFromRational);
    }
    const vertex = rational(-linear, 2n * quadratic),
        points = [ZERO_RATIONAL];
    if (compareRationals(vertex, ZERO_RATIONAL) > 0 && compareRationals(vertex, ONE_RATIONAL) < 0) points.push(vertex);
    points.push(ONE_RATIONAL);
    const roots = [];
    for (let index = 1; index < points.length; index += 1) {
        const left = points[index - 1],
            right = points[index],
            leftSign = polynomialSignAt(polynomial, left),
            rightSign = polynomialSignAt(polynomial, right);
        if (leftSign * rightSign < 0) roots.push({ exact: null, left, right, polynomial, ordinal: roots.length });
    }
    return roots;
}
function refineRoot(root) {
    if (root.exact) return;
    const middle = midpoint(root.left, root.right),
        sign = polynomialSignAt(root.polynomial, middle);
    if (sign === 0) {
        root.exact = middle;
        root.left = middle;
        root.right = middle;
    } else if (sign === polynomialSignAt(root.polynomial, root.left)) root.left = middle;
    else root.right = middle;
}
function proportional(left, right) {
    const length = Math.max(left.length, right.length);
    let leftFactor = 0n,
        rightFactor = 0n;
    for (let index = 0; index < length; index += 1) {
        const a = left[index] ?? 0n,
            b = right[index] ?? 0n;
        if (a === 0n && b === 0n) continue;
        if (a === 0n || b === 0n) return false;
        if (leftFactor === 0n) [leftFactor, rightFactor] = [a, b];
        else if (a * rightFactor !== b * leftFactor) return false;
    }
    return true;
}
function compareRoots(left, right) {
    if (left.exact && right.exact) return compareRationals(left.exact, right.exact);
    if (!left.exact && !right.exact && proportional(left.polynomial, right.polynomial))
        return left.ordinal - right.ordinal;
    for (;;) {
        if (compareRationals(left.right, right.left) < 0) return -1;
        if (compareRationals(right.right, left.left) < 0) return 1;
        if (left.exact) refineRoot(right);
        else if (right.exact) refineRoot(left);
        else if (
            left.right.numerator.toString(2).length + left.right.denominator.toString(2).length <
            right.right.numerator.toString(2).length + right.right.denominator.toString(2).length
        )
            refineRoot(left);
        else refineRoot(right);
    }
}
function signAtRoot(coefficients, root) {
    const polynomial = integerPolynomial(coefficients);
    if (polynomial.every((coefficient) => coefficient === 0n) || proportional(polynomial, root.polynomial)) return 0;
    if (root.exact) return polynomialSignAt(polynomial, root.exact);
    const vertex = polynomial.length === 3 ? rational(-polynomial[1], 2n * polynomial[2]) : null;
    for (;;) {
        const left = polynomialSignAt(polynomial, root.left),
            right = polynomialSignAt(polynomial, root.right),
            vertexOutside =
                !vertex || compareRationals(vertex, root.left) <= 0 || compareRationals(vertex, root.right) >= 0;
        if (left !== 0 && left === right && vertexOutside) return left;
        refineRoot(root);
    }
}
function rootNumber(root) {
    if (root.exact) return Number(root.exact.numerator) / Number(root.exact.denominator);
    for (;;) {
        const left = Number(root.left.numerator) / Number(root.left.denominator),
            right = Number(root.right.numerator) / Number(root.right.denominator);
        if (Object.is(left, right)) return left;
        refineRoot(root);
    }
}

function movingOrientation(startA, endA, startB, endB, point) {
    const ax = endA.x - startA.x,
        ay = endA.y - startA.y,
        bx = endB.x - startB.x,
        by = endB.y - startB.y,
        dx0 = startB.x - startA.x,
        dx1 = bx - ax,
        dy0 = startB.y - startA.y,
        dy1 = by - ay,
        ex0 = point.x - startA.x,
        ex1 = -ax,
        ey0 = point.y - startA.y,
        ey1 = -ay;
    return [dx0 * ey0 - dy0 * ex0, dx0 * ey1 + dx1 * ey0 - dy0 * ex1 - dy1 * ex0, dx1 * ey1 - dy1 * ex1];
}

function fixedOrientation(left, right, start, end) {
    const dx = right.x - left.x,
        dy = right.y - left.y;
    return [dx * (start.y - left.y) - dy * (start.x - left.x), dx * (end.y - start.y) - dy * (end.x - start.x), 0];
}

function affineDot(leftStart, leftEnd, rightStart, rightEnd) {
    const lx = leftStart.x,
        ly = leftStart.y,
        ldx = leftEnd.x - lx,
        ldy = leftEnd.y - ly,
        rx = rightStart.x,
        ry = rightStart.y,
        rdx = rightEnd.x - rx,
        rdy = rightEnd.y - ry;
    return [lx * rx + ly * ry, lx * rdx + ldx * rx + ly * rdy + ldy * ry, ldx * rdx + ldy * rdy];
}
const subtract = (left, right) => ({ x: left.x - right.x, y: left.y - right.y });
function projectionPolynomials(startA, endA, startB, endB, left, right) {
    const fixed = subtract(right, left),
        fixedReverse = subtract(left, right),
        edgeStart = subtract(startB, startA),
        edgeEnd = subtract(endB, endA),
        edgeReverseStart = subtract(startA, startB),
        edgeReverseEnd = subtract(endA, endB);
    return [
        affineDot(subtract(left, startA), subtract(left, endA), edgeStart, edgeEnd),
        affineDot(subtract(left, startB), subtract(left, endB), edgeReverseStart, edgeReverseEnd),
        affineDot(subtract(right, startA), subtract(right, endA), edgeStart, edgeEnd),
        affineDot(subtract(right, startB), subtract(right, endB), edgeReverseStart, edgeReverseEnd),
        affineDot(subtract(startA, left), subtract(endA, left), fixed, fixed),
        affineDot(subtract(startA, right), subtract(endA, right), fixedReverse, fixedReverse),
        affineDot(subtract(startB, left), subtract(endB, left), fixed, fixed),
        affineDot(subtract(startB, right), subtract(endB, right), fixedReverse, fixedReverse),
    ];
}

function segmentsIntersectAtRoot(startA, endA, startB, endB, segment, root) {
    const orientations = [
            movingOrientation(startA, endA, startB, endB, segment[0]),
            movingOrientation(startA, endA, startB, endB, segment[1]),
            fixedOrientation(segment[0], segment[1], startA, endA),
            fixedOrientation(segment[0], segment[1], startB, endB),
        ],
        signs = orientations.map((polynomial) => signAtRoot(polynomial, root)),
        projections = projectionPolynomials(startA, endA, startB, endB, segment[0], segment[1]),
        on = (first, second) => signAtRoot(projections[first], root) >= 0 && signAtRoot(projections[second], root) >= 0;
    return (
        (signs[0] * signs[1] < 0 && signs[2] * signs[3] < 0) ||
        (signs[0] === 0 && on(0, 1)) ||
        (signs[1] === 0 && on(2, 3)) ||
        (signs[2] === 0 && on(4, 5)) ||
        (signs[3] === 0 && on(6, 7))
    );
}

function segmentContactAt(leftHull, rightHull, segment) {
    const earliest = [];
    for (let index = 0; index < leftHull.length; index += 1) {
        const next = (index + 1) % leftHull.length;
        const polynomials = [
            movingOrientation(leftHull[index], rightHull[index], leftHull[next], rightHull[next], segment[0]),
            movingOrientation(leftHull[index], rightHull[index], leftHull[next], rightHull[next], segment[1]),
            fixedOrientation(segment[0], segment[1], leftHull[index], rightHull[index]),
            fixedOrientation(segment[0], segment[1], leftHull[next], rightHull[next]),
            ...projectionPolynomials(
                leftHull[index],
                rightHull[index],
                leftHull[next],
                rightHull[next],
                segment[0],
                segment[1],
            ),
        ];
        const roots = [rootFromRational(ZERO_RATIONAL), rootFromRational(ONE_RATIONAL)];
        for (const polynomial of polynomials) roots.push(...rootsInUnit(polynomial));
        roots.sort(compareRoots);
        for (const root of roots)
            if (
                segmentsIntersectAtRoot(
                    leftHull[index],
                    rightHull[index],
                    leftHull[next],
                    rightHull[next],
                    segment,
                    root,
                ) &&
                (!earliest.length || compareRoots(root, earliest[0]) < 0)
            )
                earliest.splice(0, 1, root);
    }
    if (!earliest.length) return null;
    const time = rootNumber(earliest[0]);
    return { hull: mixHull(leftHull, rightHull, time), root: earliest[0], time };
}

export function exactRouteSegmentContactTime(leftHull, rightHull, segment) {
    return segmentContactAt(leftHull, rightHull, segment)?.time ?? null;
}

function contactForCandidate(leftHull, rightHull, candidate) {
    if (candidateDistanceSquared(leftHull, candidate) === 0)
        return { hull: leftHull, root: rootFromRational(ZERO_RATIONAL), time: 0 };
    const segments = candidate.polygon
        ? candidate.polygon.map((point, index) => [point, candidate.polygon[(index + 1) % candidate.polygon.length]])
        : [candidate.segment];
    let earliest = null;
    for (const segment of segments) {
        const contact = segmentContactAt(leftHull, rightHull, segment);
        if (contact && (!earliest || compareRoots(contact.root, earliest.root) < 0)) earliest = contact;
    }
    return earliest;
}

function* knots(previous, angularTravel) {
    yield { angle: previous.angle, time: 0 };
    const endpoint = previous.angle + angularTravel;
    if (angularTravel > 0)
        for (let angle = Math.floor(previous.angle) + 1; angle <= Math.ceil(endpoint) - 1; angle += 1)
            yield { angle, time: (angle - previous.angle) / angularTravel };
    else if (angularTravel < 0)
        for (let angle = Math.ceil(previous.angle) - 1; angle >= Math.floor(endpoint) + 1; angle -= 1)
            yield { angle, time: (angle - previous.angle) / angularTravel };
    yield { angle: endpoint, time: 1 };
}

const mixPose = (left, right, time, angle) => ({
    ...Object.fromEntries(
        ["x", "y", "vx", "vy", "angularVelocity"].map((key) => [key, left[key] + (right[key] - left[key]) * time]),
    ),
    angle,
});

export function classifyRouteSweep(previous, next, angularTravel, candidates, target) {
    let prior = null;
    for (const knot of knots(previous, angularTravel)) {
        const center = mixPose(previous, next, knot.time, knot.angle),
            current = { ...knot, hull: routeHull(center) };
        if (prior) {
            const stack = [{ left: prior.time, right: current.time, leftHull: prior.hull, rightHull: current.hull }];
            while (stack.length) {
                const interval = stack.pop(),
                    displacement = Math.max(
                        ...interval.leftHull.map((point, index) =>
                            Math.hypot(interval.rightHull[index].x - point.x, interval.rightHull[index].y - point.y),
                        ),
                    ),
                    enclosure = bounds(interval.leftHull, interval.rightHull),
                    expanded = {
                        left: enclosure.left - displacement,
                        right: enclosure.right + displacement,
                        bottom: enclosure.bottom - displacement,
                        top: enclosure.top + displacement,
                    },
                    near = candidates.filter((candidate) => overlaps(candidateBounds(candidate), expanded, MARGIN));
                if (!near.length) continue;
                if (displacement > MARGIN) {
                    const middle = (interval.left + interval.right) / 2;
                    if (middle === interval.left || middle === interval.right)
                        throw new Error("Collision midpoint did not progress");
                    const middleHull = mixHull(interval.leftHull, interval.rightHull, 0.5);
                    stack.push(
                        { left: middle, right: interval.right, leftHull: middleHull, rightHull: interval.rightHull },
                        { left: interval.left, right: middle, leftHull: interval.leftHull, rightHull: middleHull },
                    );
                    continue;
                }
                const middleHull = mixHull(interval.leftHull, interval.rightHull, 0.5),
                    detected = near.filter((candidate) =>
                        [interval.leftHull, middleHull, interval.rightHull].some(
                            (hull) => candidateDistanceSquared(hull, candidate) <= MARGIN ** 2,
                        ),
                    ),
                    contacts = [];
                for (const candidate of detected) {
                    const contact = contactForCandidate(interval.leftHull, interval.rightHull, candidate);
                    if (contact)
                        contacts.push({
                            candidate,
                            hull: contact.hull,
                            root: contact.root,
                            time: interval.left + (interval.right - interval.left) * contact.time,
                        });
                }
                contacts.sort((a, b) => compareRoots(a.root, b.root) || a.candidate.priority - b.candidate.priority);
                if (contacts.length) {
                    const contact = contacts[0],
                        pose = mixPose(previous, next, contact.time, previous.angle + angularTravel * contact.time);
                    if (!contact.candidate.target)
                        return { classification: "unsafe", cause: contact.candidate.cause, pose };
                    const feet = contact.hull.slice(0, 2),
                        safe =
                            pose.vy <= 0 &&
                            feet.every((foot) => foot.x >= target.platformLeft && foot.x <= target.platformRight) &&
                            Math.abs(pose.vx) <= 2.2 &&
                            Math.abs(pose.vy) <= 3.6 &&
                            Math.abs(normalizeDegrees(pose.angle)) <= 18 &&
                            Math.abs(pose.angularVelocity) <= 26;
                    return {
                        classification: safe ? "safe" : "unsafe",
                        cause: safe ? "target" : "target-envelope",
                        pose,
                    };
                }
            }
        }
        prior = current;
    }
    return null;
}
