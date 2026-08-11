import {
    advanceCue,
    advanceMissionSequence,
    advanceSimulation,
    clearSimulationInput,
    createCueState,
    createPreflightModel,
    createSimulationClock,
    enqueueInputEdge,
    GRAVITY,
    mixDigitalInput,
    mixEngineRequests,
    plumeForThrust,
    pointerEngineRequests,
    removeQueuedInputEdges,
    resetSimulationAccumulator,
    settleCue,
    transitionMission,
    updateRetention,
} from "./lander-model.js";
import { cameraLeftForPose, CHUNK_WIDTH, mixUint32, terrainPath,
    terrainVerticesForRange, targetIsOffscreen } from "./lander-world.js";

const SVG_NAMESPACE = document.querySelector("#lander-scene")?.namespaceURI;
const ACTIVE_STATES = new Set(["flying", "landed", "deploying", "powering", "launching", "crashing", "failed", "generation-error"]);
const FLIGHT_CODES = new Set(["Space", "ArrowUp", "ArrowLeft", "ArrowRight", "KeyH", "KeyL"]);
const ZERO_INPUT = Object.freeze({ left: 0, right: 0, vectorAngle: 0 });

function svg(name, attributes = {}) {
    if (!SVG_NAMESPACE) throw new Error("SVG namespace is unavailable");
    const element = document.createElementNS(SVG_NAMESPACE, name);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    return element;
}

function eventTime(event) {
    return Number.isFinite(event.timeStamp) ? event.timeStamp : performance.now();
}

function unmodified(event, allowShift = false) {
    return !event.ctrlKey && !event.altKey && !event.metaKey && (allowShift || !event.shiftKey);
}

function physicalControl(event) {
    if (["Space", "ArrowUp", "ArrowLeft", "ArrowRight"].includes(event.code)) return event.code;
    if (event.key.toLowerCase() === "h") return "KeyH";
    if (event.key.toLowerCase() === "l") return "KeyL";
    return null;
}

function isEditable(target) {
    return target instanceof Element && Boolean(target.closest("a, button, input, select, textarea, [contenteditable='true']"));
}

function freshSeed() {
    if (globalThis.crypto?.getRandomValues) {
        const words = new Uint32Array(2);
        globalThis.crypto.getRandomValues(words);
        const rotated = ((words[1] << 13) | (words[1] >>> 19)) >>> 0;
        return mixUint32(words[0] ^ rotated);
    }
    const now = Date.now() >>> 0;
    const fine = Math.floor(performance.now() * 1000) >>> 0;
    return mixUint32(now ^ fine);
}

function createSiteGroup(site) {
    const group = svg("g", { class: "lander-site", "data-site-id": site.id });
    group.append(svg("rect", { class: "landing-platform" }), svg("path", { class: "helipad-mark" }),
        svg("path", { class: "platform-supports" }),
        svg("path", { class: "gas-can", d: "M-7 0h14v15H-7Zm4-5h7v5h-7Zm10 7h4v8H7" }),
        svg("path", { class: "noc-building" }), svg("path", { class: "noc-building noc-entry" }));
    const battery = svg("g", { class: "noc-battery" });
    battery.append(svg("rect", { rx: 2 }), svg("path", { class: "battery-terminal" }));
    for (let index = 1; index <= 4; index += 1) battery.append(svg("path", { class: `battery-bar battery-bar-${index}` }));
    group.append(battery);
    group.append(svg("path", { class: "noc-antenna antenna-mast" }),
        svg("circle", { class: "noc-antenna antenna-head", r: 4 }),
        svg("path", { class: "noc-antenna antenna-signal" }));
    return group;
}

function positionSite(group, site) {
    const left = site.platformLeft * 10;
    const right = site.platformRight * 10;
    const top = 548 - site.platformTop * 10;
    const bottom = 548 - site.platformBottom * 10;
    const center = site.center * 10;
    group.dataset.can = site.canCollected ? "collected" : "present";
    group.dataset.power = site.powered ? "on" : "off";
    group.dataset.nocStage = String(site.nocStage ?? (site.powered ? 5 : 0));
    const deck = group.querySelector(".landing-platform");
    deck.setAttribute("x", left); deck.setAttribute("y", top); deck.setAttribute("width", right - left); deck.setAttribute("height", bottom - top);
    group.querySelector(".helipad-mark").setAttribute("d", `M${center - 9} ${top + 1.7}v-20m18 20v-20m-18 10h18`);
    const shelf = top + 8;
    const supports = [`M${left} ${bottom}H${right}V${shelf}H${left}Z`];
    for (let bay = 0; bay < 6; bay += 1) {
        supports.push(`M${left + 16 * bay} ${bottom}L${left + 16 * (bay + 1)} ${shelf}`,
            `M${left + 16 * bay} ${shelf}L${left + 16 * (bay + 1)} ${bottom}`);
    }
    group.querySelector(".platform-supports").setAttribute("d", supports.join(""));
    group.querySelector(".gas-can").setAttribute("transform", `translate(${center + 30} ${top - 15})`);
    const buildingLeft = right + 20;
    const roof = top - 72;
    const foundation = 548 - (site.foundationBottom ?? site.platformTop - 0.8) * 10;
    group.querySelector(".noc-building").setAttribute("d", `M${buildingLeft} ${foundation}V${roof}h70V${foundation}Z`);
    group.querySelector(".noc-entry").setAttribute("d", `M${buildingLeft} ${top - 18}h13v18h-13Z`);
    const battery = group.querySelector(".noc-battery");
    battery.querySelector("rect").setAttribute("x", buildingLeft + 24); battery.querySelector("rect").setAttribute("y", roof + 16);
    battery.querySelector("rect").setAttribute("width", 22); battery.querySelector("rect").setAttribute("height", 40);
    battery.querySelector(".battery-terminal").setAttribute("d", `M${buildingLeft + 30} ${roof + 16}v-6h10v6`);
    const barTops = [46, 38, 30, 22];
    for (let index = 1; index <= 4; index += 1) {
        battery.querySelector(`.battery-bar-${index}`).setAttribute("d",
            `M${buildingLeft + 29} ${roof + barTops[index - 1]}h12v5h-12Z`);
    }
    group.querySelector(".antenna-mast").setAttribute("d", `M${buildingLeft + 35} ${roof}v-32`);
    group.querySelector(".antenna-head").setAttribute("cx", buildingLeft + 35);
    group.querySelector(".antenna-head").setAttribute("cy", roof - 34);
    group.querySelector(".antenna-signal").setAttribute("d", `M${buildingLeft + 44} ${roof - 38}a15 15 0 0 1 0 16m8-24a26 26 0 0 1 0 32`);
}

export class LanderGameController {
    constructor(root, cleanups = [], snapshot = root?.cloneNode(true)) {
        if (!root) throw new Error("Lunar deployment game root is missing");
        this.root = root;
        this.pristine = snapshot;
        for (const id of ["lander-scene-shell", "lander-scene", "lander-start", "lander-fuel", "lander-fuel-value",
            "lander-target-direction", "lander-controls", "lander-actions", "lander-exit", "lander-restart", "lander-status",
            "terrain-layer", "site-layer", "debris-layer", "mission-agent"]) {
            const name = id.replaceAll("-", "_");
            this[name] = root.querySelector(`#${id}`);
            if (!this[name]) throw new Error(`Missing #${id}`);
        }
        this.model = createPreflightModel();
        this.clock = createSimulationClock();
        this.heldKeys = new Set();
        this.pointerInput = ZERO_INPUT;
        this.pointer = null;
        this.pointerToken = 0;
        this.completedPointer = null;
        this.releasedCapture = null;
        this.pulseTimer = null;
        this.frameId = null;
        this.previousFrame = null;
        this.worldWindowKey = null;
        this.paused = document.hidden;
        this.destroyed = false;
        this.cleanups = cleanups;
        this.motion = matchMedia("(prefers-reduced-motion: reduce)");
        this.cue = createCueState(this.motion.matches);
        this.cleanups.push(() => {
            if (this.frameId !== null) cancelAnimationFrame(this.frameId);
            if (this.pulseTimer !== null) clearTimeout(this.pulseTimer);
        });
        this.installListeners();
        this.render();
        if (!this.paused) this.requestFrame();
        this.lander_start.hidden = false;
        this.lander_start.disabled = false;
    }

    listen(target, type, listener, options) {
        target.addEventListener(type, listener, options);
        this.cleanups.push(() => target.removeEventListener(type, listener, options));
    }

    installListeners() {
        this.listen(this.lander_start, "click", () => this.start(false, performance.now()));
        this.listen(this.lander_exit, "click", () => this.exit());
        this.listen(this.lander_restart, "click", () => this.restart());
        this.listen(document, "keydown", (event) => this.onKeyDown(event));
        this.listen(document, "keyup", (event) => this.onKeyUp(event));
        this.listen(window, "blur", () => this.clearAllInput(performance.now()));
        this.listen(this.lander_scene_shell, "focusout", () => this.clearAllInput(performance.now()));
        for (const type of ["pointerdown", "pointermove", "pointerup", "pointercancel", "lostpointercapture"]) {
            this.listen(this.lander_scene_shell, type, (event) => this.onPointer(event));
        }
        this.listen(document, "visibilitychange", () => this.onVisibilityChange());
        this.listen(this.motion, "change", (event) => this.onMotionChange(event));
    }

    queueInput(timestamp, token = null) {
        const held = Object.fromEntries([...this.heldKeys].map((key) => [key, true]));
        const request = mixEngineRequests(mixDigitalInput(held), this.pointerInput);
        const physical = Object.freeze({ heldCodes: Object.freeze([...this.heldKeys].sort()),
            pointer: this.pointer ? Object.freeze({ active: true, id: this.pointer.id,
                anchorX: this.pointer.x, currentX: this.pointer.currentX,
                token: this.pointer.token }) : Object.freeze({ active: false }),
            completedPointerToken: this.completedPointer?.token ?? null,
            pulseDeadline: this.completedPointer?.deadline ?? null });
        this.clock = enqueueInputEdge(this.clock, { timestamp, ...request, token, physical });
    }

    start(holdSpace, timestamp) {
        if (this.model.state !== "preflight") return;
        this.cue = settleCue();
        this.model = updateRetention(transitionMission(this.model, "START", { seed: freshSeed(), reducedMotion: this.motion.matches }));
        this.clock = createSimulationClock();
        this.previousFrame = null;
        this.heldKeys.clear();
        if (holdSpace) { this.heldKeys.add("Space"); this.queueInput(timestamp); }
        this.lander_start.hidden = true; this.lander_start.disabled = true;
        this.lander_fuel.hidden = false; this.lander_controls.hidden = false; this.lander_actions.hidden = false;
        this.lander_exit.disabled = false;
        this.lander_scene_shell.tabIndex = 0;
        this.lander_scene_shell.setAttribute("role", "application");
        this.lander_scene_shell.setAttribute("aria-label", "Lunar deployment game");
        this.lander_scene_shell.setAttribute("aria-describedby", "lander-controls lander-fuel lander-target-direction lander-status");
        this.lander_scene.setAttribute("aria-hidden", "true");
        this.lander_scene_shell.focus({ preventScroll: true });
        this.render(); this.requestFrame();
    }

    exit() {
        if (this.model.state === "preflight") return;
        this.clearAllInput(performance.now());
        this.model = transitionMission(this.model, "EXIT");
        this.clock = createSimulationClock(); this.previousFrame = null;
        this.lander_scene_shell.tabIndex = -1;
        for (const attribute of ["role", "aria-label", "aria-describedby"]) this.lander_scene_shell.removeAttribute(attribute);
        this.lander_scene.removeAttribute("aria-hidden");
        this.lander_fuel.hidden = true; this.lander_target_direction.hidden = true; this.lander_controls.hidden = true;
        this.lander_actions.hidden = true; this.lander_exit.disabled = true;
        this.lander_restart.hidden = true; this.lander_restart.disabled = true;
        this.lander_start.hidden = false; this.lander_start.disabled = false;
        this.lander_status.textContent = "";
        const staticTerrain = this.pristine.querySelector("#terrain-layer");
        const staticSites = this.pristine.querySelector("#site-layer");
        this.terrain_layer.replaceChildren(...[...staticTerrain.children].map((node) => node.cloneNode(true)));
        this.site_layer.replaceChildren(...[...staticSites.children].map((node) => node.cloneNode(true)));
        this.debris_layer.replaceChildren(); this.worldWindowKey = null;
        for (const property of ["--agent-x", "--agent-y", "--crash-x", "--crash-y", "--crash-progress"]) {
            this.root.style.removeProperty(property);
        }
        this.render(); this.lander_start.focus({ preventScroll: true }); this.requestFrame();
    }

    restart() {
        if (this.model.state !== "failed") return;
        this.clearAllInput(performance.now());
        this.model = updateRetention(transitionMission(this.model, "RESTART"));
        this.clock = createSimulationClock(); this.previousFrame = null;
        this.lander_restart.hidden = true; this.lander_restart.disabled = true;
        this.render(); this.lander_scene_shell.focus({ preventScroll: true }); this.requestFrame();
    }

    activePath(event) { return ACTIVE_STATES.has(this.model.state) && event.composedPath().includes(this.lander_scene_shell); }

    onKeyDown(event) {
        if (this.model.state === "preflight") {
            const acceptedTargets = [document.body, this.root, this.lander_scene_shell, this.lander_scene];
            if (event.code === "Space" && !event.repeat && unmodified(event) && acceptedTargets.includes(event.target) && !isEditable(event.target)) {
                event.preventDefault(); this.start(true, eventTime(event));
            }
            return;
        }
        if (!this.activePath(event)) return;
        if (event.key === "Escape" && unmodified(event)) { event.preventDefault(); this.exit(); return; }
        if (event.key.toLowerCase() === "r" && unmodified(event) && this.model.state === "failed") { event.preventDefault(); this.restart(); return; }
        const control = physicalControl(event);
        if (this.model.state !== "flying" || !control || !unmodified(event, true)) return;
        event.preventDefault();
        if (event.repeat || this.heldKeys.has(control)) return;
        this.heldKeys.add(control); this.queueInput(eventTime(event));
    }

    onKeyUp(event) {
        const control = physicalControl(event);
        if (!control || !this.heldKeys.delete(control)) return;
        if (this.model.state === "flying") this.queueInput(eventTime(event));
    }

    onPointer(event) {
        if (event.type === "lostpointercapture") {
            const released = this.releasedCapture;
            if (!released || released.id !== event.pointerId) {
                this.finishPointer(this.pointer?.token ?? this.completedPointer?.token, eventTime(event), true);
                return;
            }
            this.releasedCapture = null;
            if (this.completedPointer?.token === released.token &&
                released.releasedAt < this.completedPointer.deadline) return;
            this.finishPointer(released.token, eventTime(event), true);
            return;
        }
        if (event.type === "pointerdown") {
            if (this.model.state !== "flying" || !event.isPrimary || event.button !== 0 || this.pointer) return;
            event.preventDefault();
            const timestamp = eventTime(event);
            if (this.completedPointer) {
                this.completePointerPulse(this.completedPointer.token, this.completedPointer.deadline, timestamp);
            }
            const token = ++this.pointerToken;
            this.pointer = { id: event.pointerId, x: event.clientX, y: event.clientY, currentX: event.clientX,
                started: timestamp, token };
            this.lander_scene_shell.setPointerCapture(event.pointerId);
            this.pointerInput = { left: 0.72, right: 0.72 }; this.queueInput(timestamp, token); return;
        }
        if (!this.pointer || event.pointerId !== this.pointer.id) return;
        event.preventDefault();
        if (event.type === "pointermove") {
            this.pointer.currentX = event.clientX;
            this.pointerInput = pointerEngineRequests(event.clientX - this.pointer.x, this.lander_scene.getBoundingClientRect().width);
            this.queueInput(eventTime(event), this.pointer.token); return;
        }
        if (event.type === "pointerup") {
            const pointer = this.pointer;
            const elapsed = eventTime(event) - pointer.started;
            const distance = Math.hypot(event.clientX - pointer.x, event.clientY - pointer.y);
            if (elapsed <= 180 && distance <= 10 && elapsed < 140) {
                const deadline = pointer.started + 140;
                this.completedPointer = { token: pointer.token, deadline };
                this.releasedCapture = { id: pointer.id, token: pointer.token, releasedAt: eventTime(event) };
                this.pointer = null;
                this.pointerInput = { left: 0.72, right: 0.72 };
                this.queueInput(eventTime(event), pointer.token);
                this.pulseTimer = setTimeout(() => this.completePointerPulse(pointer.token, deadline, deadline),
                    Math.max(0, deadline - eventTime(event)));
                if (this.lander_scene_shell.hasPointerCapture(pointer.id)) {
                    this.lander_scene_shell.releasePointerCapture(pointer.id);
                }
                return;
            }
        }
        this.finishPointer(this.pointer.token, eventTime(event), true);
    }

    finishPointer(token, timestamp, prune = false) {
        if (token === undefined || token === null) return;
        if (this.pointer?.token !== token && this.completedPointer?.token !== token &&
            this.releasedCapture?.token !== token) return;
        if (this.pulseTimer !== null) { clearTimeout(this.pulseTimer); this.pulseTimer = null; }
        if (this.pointer?.token === token) this.pointer = null;
        if (this.completedPointer?.token === token) this.completedPointer = null;
        if (this.releasedCapture?.token === token) this.releasedCapture = null;
        this.pointerInput = ZERO_INPUT;
        if (prune) this.clock = removeQueuedInputEdges(this.clock, token);
        if (this.model.state === "flying") this.queueInput(timestamp);
    }

    completePointerPulse(token, deadline, timestamp) {
        if (this.completedPointer?.token !== token || this.completedPointer.deadline !== deadline) return;
        this.finishPointer(token, timestamp);
    }

    clearAllInput(timestamp) {
        this.heldKeys.clear();
        const pointer = this.pointer;
        const token = pointer?.token ?? this.completedPointer?.token;
        this.pointer = null; this.completedPointer = null; this.releasedCapture = null;
        if (this.pulseTimer !== null) { clearTimeout(this.pulseTimer); this.pulseTimer = null; }
        this.pointerInput = ZERO_INPUT;
        if (token !== undefined) this.clock = removeQueuedInputEdges(this.clock, token);
        if (pointer && this.lander_scene_shell.hasPointerCapture(pointer.id)) {
            this.lander_scene_shell.releasePointerCapture(pointer.id);
        }
        this.clock = clearSimulationInput(this.clock, timestamp);
        this.model = { ...this.model, commanded: { ...ZERO_INPUT } };
    }

    onVisibilityChange() {
        this.paused = document.hidden; this.root.dataset.paused = String(this.paused);
        if (this.paused) {
            if (this.frameId !== null) cancelAnimationFrame(this.frameId);
            this.frameId = null; this.clearAllInput(performance.now());
            this.clock = resetSimulationAccumulator(this.clock, null); this.previousFrame = null;
        } else { this.clock = createSimulationClock(); this.previousFrame = null; this.requestFrame(); }
    }

    onMotionChange(event) {
        this.root.dataset.reducedMotion = String(event.matches);
        this.model = { ...this.model, reducedMotion: event.matches };
        if (event.matches) { this.cue = settleCue(); this.model = advanceMissionSequence(this.model, 3.1, true); this.render(); }
    }

    requestFrame() {
        if (this.frameId === null && !this.paused && !this.destroyed) this.frameId = requestAnimationFrame((time) => this.frame(time));
    }

    frame(timestamp) {
        this.frameId = null;
        const elapsed = this.previousFrame === null ? 0 : Math.max(0, Math.min(0.1, (timestamp - this.previousFrame) / 1000));
        this.previousFrame = timestamp;
        const previousState = this.model.state;
        if (this.model.state === "preflight") this.cue = advanceCue(this.cue, elapsed);
        else if (["flying", "launching"].includes(this.model.state)) {
            const result = advanceSimulation(this.clock, this.model, timestamp);
            this.clock = result.clock; this.model = result.model;
            if (result.discarded) this.clearAllInput(timestamp);
        } else if (["landed", "deploying", "powering", "crashing"].includes(this.model.state)) {
            this.model = updateRetention(advanceMissionSequence(this.model, elapsed, this.motion.matches));
        }
        if (previousState === "flying" && this.model.state !== "flying") this.clearAllInput(timestamp);
        this.render();
        if (["flying", "landed", "deploying", "powering", "launching", "crashing"].includes(this.model.state) ||
            (this.model.state === "preflight" && this.cue.state === "running")) this.requestFrame();
    }

    reconcileWorld() {
        if (!this.model.terrainVertices) return;
        if (this.worldWindowKey === this.model.retentionKey) {
            for (const site of this.model.retainedSites) {
                const group = this.site_layer.querySelector(`[data-site-id="${site.id}"]`);
                if (group) {
                    group.dataset.can = site.canCollected ? "collected" : "present";
                    group.dataset.power = site.powered ? "on" : "off";
                    group.dataset.nocStage = String(site.nocStage ?? (site.powered ? 5 : 0));
                }
            }
            return;
        }
        const indexes = this.model.retainedChunks;
        const existingChunks = new Map([...this.terrain_layer.children].map((node) => [Number(node.dataset.chunkIndex), node]));
        for (const index of indexes) {
            let path = existingChunks.get(index);
            if (!path) { path = svg("path", { class: "terrain-chunk", "data-chunk-index": index }); this.terrain_layer.append(path); }
            const left = index * CHUNK_WIDTH; const right = left + CHUNK_WIDTH;
            const vertices = terrainVerticesForRange(this.model.terrainVertices, left, right);
            path.setAttribute("d", terrainPath(vertices)); existingChunks.delete(index);
        }
        for (const node of existingChunks.values()) node.remove();
        const existingSites = new Map([...this.site_layer.children].map((node) => [Number(node.dataset.siteId), node]));
        for (const site of this.model.retainedSites) {
            let group = existingSites.get(site.id);
            if (!group) { group = createSiteGroup(site); this.site_layer.append(group); }
            positionSite(group, site); existingSites.delete(site.id);
        }
        for (const node of existingSites.values()) node.remove();
        this.worldWindowKey = this.model.retentionKey;
    }

    renderCrash(cameraLeft) {
        if (this.model.state !== "crashing" || !this.model.crash) {
            if (this.debris_layer.children.length) this.debris_layer.replaceChildren();
            return;
        }
        const time = this.model.sequenceSeconds;
        const existing = new Map([...this.debris_layer.children].map((node) => [Number(node.dataset.fragmentId), node]));
        for (const fragment of this.model.crash.fragments) {
            const x = fragment.x + fragment.vx * time;
            const y = fragment.y + fragment.vy * time - 0.5 * GRAVITY * time * time;
            let node = existing.get(fragment.id);
            if (!node) {
                node = svg("rect", { width: 8, height: 8, fill: fragment.color, "data-fragment-id": fragment.id });
                this.debris_layer.append(node);
            }
            node.setAttribute("x", x * 10 - 4); node.setAttribute("y", 548 - y * 10 - 4);
            node.setAttribute("transform", `rotate(${fragment.angularVelocity * time} ${x * 10} ${548 - y * 10})`);
            existing.delete(fragment.id);
        }
        for (const node of existing.values()) node.remove();
        this.root.style.setProperty("--crash-x", `${(this.model.crash.pose.x - cameraLeft) * 10}px`);
        this.root.style.setProperty("--crash-y", `${548 - this.model.crash.pose.y * 10}px`);
        this.root.style.setProperty("--crash-progress", String(Math.min(1, time / 0.14)));
    }

    render() {
        const pose = this.model.pose;
        const cameraLeft = cameraLeftForPose(pose);
        const left = plumeForThrust(this.model.commanded.left); const right = plumeForThrust(this.model.commanded.right);
        this.root.dataset.missionState = this.model.state; this.root.dataset.cue = this.cue.state;
        this.root.dataset.paused = String(this.paused); this.root.dataset.reducedMotion = String(this.motion.matches);
        this.root.style.setProperty("--camera-x", `${-cameraLeft * 10}px`);
        this.root.style.setProperty("--lander-x", `${pose.x * 10}px`); this.root.style.setProperty("--lander-y", `${548 - pose.y * 10}px`);
        this.root.style.setProperty("--lander-angle", `${pose.angle}deg`);
        this.root.style.setProperty("--thrust-vector-angle", `${this.model.commanded.vectorAngle ?? 0}deg`);
        for (const [name, value] of [["left-plume-scale", left.scaleY], ["right-plume-scale", right.scaleY],
            ["left-plume-opacity", left.opacity], ["right-plume-opacity", right.opacity]]) this.root.style.setProperty(`--${name}`, String(value));
        this.reconcileWorld(); this.renderCrash(cameraLeft);
        const active = this.model.retainedSites?.find((site) => site.id === this.model.activeSiteId);
        if (this.model.agent && active) {
            const startX = this.model.touchdownPose.x + 1.1; const endX = active.platformRight + 2;
            const progress = this.model.agent.progress;
            this.root.style.setProperty("--agent-x", `${(startX + (endX - startX) * progress) * 10}px`);
            this.root.style.setProperty("--agent-y", `${548 - (active.platformTop + 0.2) * 10}px`);
        }
        const target = this.model.retainedSites?.find((site) => site.id === this.model.targetSiteId);
        const offscreen = targetIsOffscreen(target, cameraLeft);
        this.root.dataset.targetOffscreen = String(offscreen);
        this.lander_target_direction.hidden = !offscreen;
        const fuel = this.model.state === "preflight" ? "0.0" : this.model.fuel.toFixed(1);
        if (this.lander_fuel_value.value !== fuel) this.lander_fuel_value.value = fuel;
        if (this.lander_status.textContent !== this.model.status) this.lander_status.textContent = this.model.status;
        const failed = this.model.state === "failed";
        this.lander_restart.hidden = !failed; this.lander_restart.disabled = !failed;
    }

    destroy() {
        if (this.destroyed) return;
        this.destroyed = true;
        if (this.frameId !== null) { cancelAnimationFrame(this.frameId); this.frameId = null; }
        this.clearAllInput(performance.now());
        while (this.cleanups.length) { try { this.cleanups.pop()(); } catch (error) { console.error(error); } }
        this.model = createPreflightModel(); this.clock = createSimulationClock(); this.cue = settleCue();
        this.previousFrame = null; this.paused = false;
        const restored = this.pristine.cloneNode(true);
        this.root.replaceWith(restored);
        this.root = restored;
    }
}

export function initializeLanderGame(root = document.querySelector("#lander-game")) {
    if (!root) return null;
    const snapshot = root.cloneNode(true);
    const cleanups = [];
    try { return new LanderGameController(root, cleanups, snapshot); }
    catch (error) {
        console.error(error);
        while (cleanups.length) {
            try { cleanups.pop()(); } catch (cleanupError) { console.error(cleanupError); }
        }
        root.replaceWith(snapshot);
        return null;
    }
}

export const landerGameController = initializeLanderGame();
