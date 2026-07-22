// ==UserScript==
// @name         Datacat Bulk JanitorAI Character Retriever
// @namespace    https://greasyfork.org/users/1622561-flonz
// @version      1.5.2
// @description  Collect character UUIDs from JanitorAI pages and bulk-retrieve them through Datacat with retries, persistence, and result export.
// @author       Flo
// @license      MIT
// @match        https://datacat.run/*
// @match        https://janitorai.com/*
// @match        https://www.janitorai.com/*
// @icon         https://www.google.com/s2/favicons?sz=64&domain=datacat.run
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @grant        GM_listValues
// @grant        GM_addValueChangeListener
// @grant        unsafeWindow
// @run-at       document-start
// @downloadURL  https://update.greasyfork.org/scripts/586720/Datacat%20Bulk%20JanitorAI%20Character%20Retriever.user.js
// @updateURL    https://update.greasyfork.org/scripts/586720/Datacat%20Bulk%20JanitorAI%20Character%20Retriever.meta.js
// ==/UserScript==

(function () {
    "use strict";

    // GM values are shared by every tab running this userscript, even when the
    // tabs are on different origins. Keep page hooks on unsafeWindow: adding a
    // GM grant otherwise moves Tampermonkey code into its isolated sandbox.
    const pageWindow = unsafeWindow;

    if (
        pageWindow.__datacatJanitorToolsLoaded ||
        pageWindow.__datacatBulkRetrieverLoaded
    ) {
        return;
    }
    pageWindow.__datacatJanitorToolsLoaded = true;
    pageWindow.__datacatBulkRetrieverLoaded = true;

    const SCRIPT_VERSION = "1.5.2";
    const UUID_PATTERN =
        /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
    const JANITOR_COLLECTOR_STORAGE_KEY = "datacat_janitor_uuid_collector_v1";
    const JANITOR_ORIGINS = new Set([
        "https://janitorai.com",
        "https://www.janitorai.com"
    ]);
    const SHARED_KEY_PREFIX = "datacat_bulk_retriever_shared_v1:";
    const TAB_ID = `${Date.now().toString(36)}-${crypto.getRandomValues(new Uint32Array(1))[0].toString(36)}`;
    const TAB_TTL_MS = 45_000;
    const WORKER_LEASE_MS = 15_000;
    const WORKER_HEARTBEAT_MS = 5_000;
    const tabKey = `${SHARED_KEY_PREFIX}tab:${TAB_ID}`;
    const tabsRevisionKey = `${SHARED_KEY_PREFIX}tabs-revision`;
    const janitorRevisionKey = `${SHARED_KEY_PREFIX}janitor-revision`;
    const workerLeaseKey = `${SHARED_KEY_PREFIX}worker-lease`;
    const sharedJobKey = `${SHARED_KEY_PREFIX}job`;
    const datacatInputKey = `${SHARED_KEY_PREFIX}datacat-input`;
    let tabRole = "unknown";
    let workerHeartbeatId = null;

    function touchTab(extra = {}) {
        const previous = GM_getValue(tabKey, {});
        GM_setValue(tabKey, {
            ...previous,
            id: TAB_ID,
            role: tabRole,
            origin: location.origin,
            href: location.href,
            seenAt: Date.now(),
            ...extra
        });
        GM_setValue(tabsRevisionKey, { id: TAB_ID, at: Date.now() });
    }

    function activeJanitorIds() {
        const now = Date.now();
        const ids = new Set();
        for (const key of GM_listValues()) {
            if (!key.startsWith(`${SHARED_KEY_PREFIX}tab:`)) continue;
            const tab = GM_getValue(key, null);
            if (
                tab?.role !== "janitor" ||
                !Array.isArray(tab.ids) ||
                !Number.isFinite(tab.seenAt) ||
                now - tab.seenAt > TAB_TTL_MS
            ) continue;
            for (const id of tab.ids) if (typeof id === "string") ids.add(id);
        }
        return [...ids];
    }

    function activeTabCount(role) {
        const now = Date.now();
        let count = 0;
        for (const key of GM_listValues()) {
            if (!key.startsWith(`${SHARED_KEY_PREFIX}tab:`)) continue;
            const tab = GM_getValue(key, null);
            if (
                tab?.role === role &&
                Number.isFinite(tab.seenAt) &&
                now - tab.seenAt <= TAB_TTL_MS
            ) count++;
        }
        return count;
    }

    function publishJanitorIds(ids) {
        touchTab({ ids: [...ids] });
        GM_setValue(janitorRevisionKey, { id: TAB_ID, at: Date.now() });
    }

    function getWorkerLease() {
        return GM_getValue(workerLeaseKey, null);
    }

    async function acquireWorkerLease() {
        const current = getWorkerLease();
        if (current?.tabId !== TAB_ID && current?.expiresAt > Date.now()) {
            return false;
        }
        GM_setValue(workerLeaseKey, {
            tabId: TAB_ID,
            expiresAt: Date.now() + WORKER_LEASE_MS
        });
        // Let competing tabs write their candidate lease, then verify ours won.
        await new Promise((resolve) => setTimeout(resolve, 75));
        return getWorkerLease()?.tabId === TAB_ID;
    }

    function renewWorkerLease() {
        if (getWorkerLease()?.tabId !== TAB_ID) return false;
        GM_setValue(workerLeaseKey, {
            tabId: TAB_ID,
            expiresAt: Date.now() + WORKER_LEASE_MS
        });
        return true;
    }

    function releaseWorkerLease() {
        if (getWorkerLease()?.tabId === TAB_ID) GM_deleteValue(workerLeaseKey);
        if (workerHeartbeatId !== null) clearInterval(workerHeartbeatId);
        workerHeartbeatId = null;
    }

    function startWorkerHeartbeat() {
        workerHeartbeatId = setInterval(() => {
            if (!renewWorkerLease()) releaseWorkerLease();
        }, WORKER_HEARTBEAT_MS);
    }

    const hostname = location.hostname.toLowerCase();
    if (hostname === "janitorai.com" || hostname === "www.janitorai.com") {
        tabRole = "janitor";
        touchTab({ ids: [] });
        setInterval(() => touchTab(), WORKER_HEARTBEAT_MS);
        mountJanitorCollector();
        return;
    }

    if (hostname !== "datacat.run") return;
    tabRole = "datacat";
    touchTab();
    setInterval(() => touchTab(), WORKER_HEARTBEAT_MS);

    function mountJanitorCollector() {
        const collectedIds = new Set();
        let scanIntervalId = null;
        let lastScanAdded = 0;
        let savedPosition = null;

        try {
            const saved = JSON.parse(
                localStorage.getItem(JANITOR_COLLECTOR_STORAGE_KEY) || "{}"
            );
            for (const id of Array.isArray(saved.ids) ? saved.ids : []) {
                if (typeof id !== "string") continue;
                const uuid = uuidFromValue(id);
                if (uuid === id.toLowerCase()) collectedIds.add(uuid);
            }
            if (
                Number.isFinite(saved.position?.left) &&
                Number.isFinite(saved.position?.top)
            ) {
                savedPosition = saved.position;
            }
        } catch {
            /* Ignore malformed saved collector state. */
        }

        const panel = document.createElement("section");
        panel.id = "janitor-uuid-collector";
        panel.innerHTML = `
            <header id="janitor-uuid-header">
                <div>
                    <strong>Card UUID collector</strong>
                    <small>v${SCRIPT_VERSION}</small>
                </div>
                <button type="button" id="janitor-uuid-toggle" title="Minimise">−</button>
            </header>
            <div id="janitor-uuid-content">
                <div id="janitor-uuid-status" role="status">Scanning this page…</div>
                <textarea id="janitor-uuid-output" readonly spellcheck="false"
                    placeholder="Character UUIDs found on this page will appear here."></textarea>
                <div id="janitor-uuid-actions">
                    <button type="button" id="janitor-uuid-scan">Scan page</button>
                    <button type="button" id="janitor-uuid-copy" disabled>Copy UUIDs</button>
                    <button type="button" id="janitor-uuid-copy-links" disabled>Copy as Janitor links</button>
                    <button type="button" id="janitor-uuid-send" disabled>Send to Datacat</button>
                    <button type="button" id="janitor-uuid-clear" disabled>Clear</button>
                </div>
            </div>
        `;

        const style = document.createElement("style");
        style.textContent = `
            #janitor-uuid-collector {
                position: fixed; right: 18px; bottom: 18px; z-index: 2147483647;
                width: min(410px, calc(100vw - 24px)); padding: 12px;
                border: 1px solid rgba(255,255,255,.18); border-radius: 12px;
                background: rgba(18,18,22,.97); color: #f5f5f5;
                box-shadow: 0 12px 40px rgba(0,0,0,.45);
                font: 14px/1.4 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
                backdrop-filter: blur(12px);
            }
            #janitor-uuid-collector * { box-sizing: border-box; }
            #janitor-uuid-header {
                display: flex; align-items: center; justify-content: space-between;
                gap: 10px; margin-bottom: 9px; cursor: move; user-select: none;
            }
            #janitor-uuid-header > div { display: flex; align-items: baseline; gap: 7px; }
            #janitor-uuid-header small { color: #9797a3; font-size: 10px; }
            #janitor-uuid-toggle {
                width: 30px; height: 30px; padding: 0; border: 0;
                border-radius: 7px; background: #35353d; color: #fff;
                cursor: pointer; font-size: 20px;
            }
            #janitor-uuid-status {
                margin-bottom: 8px; color: #cfcfd6; font-size: 12px;
            }
            #janitor-uuid-output {
                display: block; width: 100%; height: 180px; resize: vertical;
                padding: 9px; border: 1px solid #454550; border-radius: 8px;
                outline: none; background: #111116; color: #f5f5f5;
                font: 12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;
            }
            #janitor-uuid-actions {
                display: flex; flex-wrap: wrap; gap: 7px; margin-top: 9px;
            }
            #janitor-uuid-actions button {
                flex: 1 1 30%; min-width: 0; padding: 8px 7px; border: 0; border-radius: 7px;
                background: #35353d; color: #fff; cursor: pointer; font-weight: 600;
            }
            #janitor-uuid-actions button:disabled { cursor: not-allowed; opacity: .42; }
            #janitor-uuid-collector.minimised { width: 270px; }
            #janitor-uuid-collector.minimised #janitor-uuid-content { display: none; }
            #janitor-uuid-collector.minimised #janitor-uuid-header { margin-bottom: 0; }
            @media (max-width: 480px) {
                #janitor-uuid-collector {
                    right: 8px; left: 8px; bottom: 8px; width: auto;
                }
            }
        `;

        const output = panel.querySelector("#janitor-uuid-output");
        const status = panel.querySelector("#janitor-uuid-status");
        const scanButton = panel.querySelector("#janitor-uuid-scan");
        const copyButton = panel.querySelector("#janitor-uuid-copy");
        const copyLinksButton = panel.querySelector("#janitor-uuid-copy-links");
        const sendButton = panel.querySelector("#janitor-uuid-send");
        const clearButton = panel.querySelector("#janitor-uuid-clear");
        const toggleButton = panel.querySelector("#janitor-uuid-toggle");

        const mount = () => {
            (document.head || document.documentElement).appendChild(style);
            document.body.appendChild(panel);
            if (savedPosition) {
                panel.style.right = "auto";
                panel.style.bottom = "auto";
                panel.style.left = `${Math.max(0, savedPosition.left)}px`;
                panel.style.top = `${Math.max(0, savedPosition.top)}px`;
            }
            keepCollectorVisible();
            refreshOutput();
            syncFromPeers();
            publishJanitorIds(collectedIds);
            scanPage();
        };

        if (document.body) mount();
        else document.addEventListener("DOMContentLoaded", mount, { once: true });

        enableDragging(
            panel,
            panel.querySelector("#janitor-uuid-header"),
            (rect) => persistCollectorState({
                left: Math.round(rect.left),
                top: Math.round(rect.top)
            })
        );

        function keepCollectorVisible() {
            const position = clampPanelToViewport(panel);
            if (position) persistCollectorState(position);
        }

        pageWindow.addEventListener("resize", keepCollectorVisible);

        function uuidFromValue(value) {
            const match = String(value || "").match(UUID_PATTERN);
            return match ? match[0].toLowerCase() : null;
        }

        function uuidFromCharacterLink(value) {
            try {
                const url = new URL(value, location.href);
                if (
                    url.hostname !== "janitorai.com" &&
                    url.hostname !== "www.janitorai.com"
                ) {
                    return null;
                }

                const parts = url.pathname.split("/").filter(Boolean);
                const characterIndex = parts.findIndex(
                    (part) => part.toLowerCase() === "characters"
                );
                if (characterIndex === -1 || !parts[characterIndex + 1]) {
                    return null;
                }
                return uuidFromValue(
                    decodeURIComponent(parts[characterIndex + 1])
                );
            } catch {
                return null;
            }
        }

        function refreshOutput() {
            output.value = [...collectedIds].join("\n");
            const count = collectedIds.size;
            const prefix = scanIntervalId === null ? "" : "Scanning · ";
            status.textContent = `${prefix}${count} ID${count === 1 ? "" : "s"} · +${lastScanAdded} last scan.`;
            copyButton.disabled = count === 0;
            copyLinksButton.disabled = count === 0;
            sendButton.disabled = count === 0;
            clearButton.disabled = count === 0;
        }

        function persistCollectorState(position = savedPosition) {
            savedPosition = position;
            localStorage.setItem(
                JANITOR_COLLECTOR_STORAGE_KEY,
                JSON.stringify({ ids: [...collectedIds], position: savedPosition })
            );
            publishJanitorIds(collectedIds);
        }

        // Merge in every active Janitor tab's collected IDs. Called on mount (a
        // fresh tab must pull peers that have nothing new to trigger a republish)
        // and on every peer publish.
        function syncFromPeers() {
            let changed = false;
            for (const id of activeJanitorIds()) {
                if (!collectedIds.has(id)) {
                    collectedIds.add(id);
                    changed = true;
                }
            }
            if (changed) {
                persistCollectorState();
                refreshOutput();
            }
            return changed;
        }

        GM_addValueChangeListener(janitorRevisionKey, (_key, _old, _value, remote) => {
            if (remote) syncFromPeers();
        });

        function scanPage({ refreshWhenUnchanged = false } = {}) {
            const before = collectedIds.size;

            // Janitor uses both relative and absolute links depending on the
            // page/view. Let uuidFromCharacterLink validate the origin and
            // path instead of relying on a particular href representation.
            for (const link of document.querySelectorAll("a[href]")) {
                const id = uuidFromCharacterLink(link.getAttribute("href"));
                if (id) collectedIds.add(id);
            }

            for (const card of document.querySelectorAll(
                "[data-character-id], [data-character-uuid]"
            )) {
                const id =
                    uuidFromValue(card.getAttribute("data-character-id")) ||
                    uuidFromValue(card.getAttribute("data-character-uuid"));
                if (id) collectedIds.add(id);
            }

            const added = collectedIds.size - before;
            lastScanAdded = added;
            if (added) {
                persistCollectorState();
                refreshOutput();
            } else if (refreshWhenUnchanged) {
                refreshOutput();
            }
            return added;
        }

        async function copyText(text) {
            if (!text) return;

            try {
                await navigator.clipboard.writeText(text);
            } catch {
                const fallback = document.createElement("textarea");
                fallback.value = text;
                fallback.style.position = "fixed";
                fallback.style.opacity = "0";
                document.body.appendChild(fallback);
                fallback.select();
                document.execCommand("copy");
                fallback.remove();
            }
        }

        async function copyIds() {
            await copyText([...collectedIds].join("\n"));
            status.textContent = `Copied ${collectedIds.size} UUID${collectedIds.size === 1 ? "" : "s"}.`;
        }

        async function copyJanitorLinks() {
            const links = [...collectedIds]
                .map((id) => `https://janitorai.com/characters/${id}`)
                .join("\n");
            await copyText(links);
            status.textContent = `Copied ${collectedIds.size} Janitor link${collectedIds.size === 1 ? "" : "s"}.`;
        }

        function sendToDatacat() {
            const ids = [...collectedIds];
            if (!ids.length) return;

            // Existing Datacat instances receive these IDs through GM storage.
            // Browser tabs cannot be focused unless this page owns their Window
            // reference, so do not create a second retrieval panel needlessly.
            publishJanitorIds(ids);
            const openDatacatTabs = activeTabCount("datacat");
            if (openDatacatTabs) {
                status.textContent = `Sent ${ids.length} ID${ids.length === 1 ? "" : "s"} to ${openDatacatTabs} active Datacat tab${openDatacatTabs === 1 ? "" : "s"}.`;
                return;
            }

            const importUrl = new URL("https://datacat.run/");
            importUrl.hash = new URLSearchParams({
                janitorIds: ids.join(",")
            }).toString();
            const target = pageWindow.open(
                importUrl.href,
                "datacat-janitor-import"
            );
            if (!target) {
                status.textContent = "Could not open Datacat. Allow pop-ups, then try again.";
                return;
            }
            target.focus?.();
            status.textContent = `Opened Datacat with ${ids.length} ID${ids.length === 1 ? "" : "s"} ready to import.`;
        }

        function toggleScanning() {
            if (scanIntervalId !== null) {
                clearInterval(scanIntervalId);
                scanIntervalId = null;
                scanButton.textContent = "Scan page";
                refreshOutput();
                return;
            }

            scanIntervalId = setInterval(scanPage, 500);
            scanButton.textContent = "Stop scanning";
            refreshOutput();
            scanPage();
        }

        scanButton.addEventListener("click", toggleScanning);
        copyButton.addEventListener("click", () => {
            copyIds().catch((error) => {
                status.textContent = `Could not copy UUIDs: ${String(error)}`;
            });
        });
        copyLinksButton.addEventListener("click", () => {
            copyJanitorLinks().catch((error) => {
                status.textContent = `Could not copy Janitor links: ${String(error)}`;
            });
        });
        sendButton.addEventListener("click", sendToDatacat);
        clearButton.addEventListener("click", () => {
            collectedIds.clear();
            lastScanAdded = 0;
            persistCollectorState();
            refreshOutput();
        });
        toggleButton.addEventListener("click", () => {
            const minimised = panel.classList.toggle("minimised");
            toggleButton.textContent = minimised ? "+" : "−";
            toggleButton.title = minimised ? "Expand" : "Minimise";
        });
    }

    const CONFIG = Object.freeze({
        storageKey: "datacat_bulk_retriever_v1",
        jobStorageKey: "datacat_bulk_retriever_job_v1",
        existingCacheStorageKey: "datacat_bulk_retriever_existing_ids_v1",
        defaultDelayMs: 5000,
        defaultRetries: 3,
        requestTimeoutMs: 45000,
        fallbackRateLimitMs: 15000,
        maxBackoffMs: 120000,
        existenceCheckConcurrency: 2,
        existingCacheMaxEntries: 5000,
        maxLogLines: 400,
        saveDebounceMs: 400,
        liveQueueSettleMs: 500
    });

    const state = {
        running: false,
        paused: false,
        stopping: false,
        requestControllers: new Set(),
        sleepCancel: null,
        results: [],
        pendingIds: [],
        currentIndex: 0,
        activeIndex: null,
        startedAtMs: null
    };

    let latestQueueCapacity = null;
    let saveTimer = null;
    let existingCacheSaveTimer = null;
    /** Filled in after panel mount; safe optional access before then. */
    let elements = null;

    const $ = (selector, root = document) => root.querySelector(selector);

    const originalFetch = pageWindow.fetch.bind(pageWindow);

    pageWindow.fetch = async function (...args) {
        const request = args[0];
        const options = args[1] || {};
        const url =
            typeof request === "string" ? request : request?.url;

        const response = await originalFetch(...args);

        if (url?.includes("/api/state/snapshot")) {
            response
                .clone()
                .json()
                .then((data) => {
                    pageWindow.dispatchEvent(
                        new CustomEvent("datacat:snapshot", { detail: data })
                    );
                })
                .catch((error) => {
                    console.warn(
                        "[Datacat snapshot] Could not read response:",
                        error
                    );
                });
        }

        return response;
    };

    pageWindow.addEventListener("datacat:snapshot", (event) => {
        latestQueueCapacity =
            event.detail?.extraction?.queueCapacity ?? null;

        if (!elements?.queueCount) return;

        elements.queueCount.textContent = latestQueueCapacity
            ? `Queue: ${latestQueueCapacity.pendingCount ?? 0} / ${latestQueueCapacity.limit ?? "∞"}`
            : "Queue: unavailable";
    });

    function loadSettings() {
        try {
            return JSON.parse(localStorage.getItem(CONFIG.storageKey) || "{}");
        } catch {
            return {};
        }
    }

    function saveSettings(patch = {}) {
        const previous = loadSettings();
        localStorage.setItem(
            CONFIG.storageKey,
            JSON.stringify({ ...previous, ...patch })
        );
    }

    function saveSettingsDebounced(patch = {}) {
        clearTimeout(saveTimer);
        saveTimer = setTimeout(() => saveSettings(patch), CONFIG.saveDebounceMs);
    }

    function loadExistingIdCache() {
        try {
            const cached = JSON.parse(
                localStorage.getItem(CONFIG.existingCacheStorageKey) || "[]"
            );
            const ids = Array.isArray(cached) ? cached : [];
            const cache = new Map();
            for (const id of ids) {
                if (typeof id === "string") cache.set(id, true);
            }
            while (cache.size > CONFIG.existingCacheMaxEntries) {
                cache.delete(cache.keys().next().value);
            }
            return cache;
        } catch {
            return new Map();
        }
    }

    const existingIdCache = loadExistingIdCache();

    function saveExistingIdCache() {
        existingCacheSaveTimer = null;
        localStorage.setItem(
            CONFIG.existingCacheStorageKey,
            JSON.stringify([...existingIdCache.keys()])
        );
    }

    function cacheExistingId(id) {
        existingIdCache.delete(id);
        existingIdCache.set(id, true);
        while (existingIdCache.size > CONFIG.existingCacheMaxEntries) {
            existingIdCache.delete(existingIdCache.keys().next().value);
        }
        if (existingCacheSaveTimer === null) {
            existingCacheSaveTimer = setTimeout(saveExistingIdCache, 250);
        }
    }

    function loadJob() {
        return GM_getValue(sharedJobKey, null);
    }

    function saveJob(job) {
        GM_setValue(sharedJobKey, job);
    }

    function persistJobProgress() {
        if (!state.running && !state.pendingIds.length) {
            saveJob(null);
            return;
        }
        saveJob({
            workerTabId: TAB_ID,
            pendingIds: state.pendingIds,
            currentIndex: state.currentIndex,
            results: state.results,
            options: readOptions(),
            updatedAt: Date.now()
        });
    }

    function getAuthHeaders() {
        const deviceToken = localStorage.getItem("liberator_device_token");
        const sessionToken = localStorage.getItem("liberator_session_token");

        if (!deviceToken || !sessionToken) {
            throw new Error(
                "Datacat authentication tokens were not found in localStorage. Log in or refresh the page first."
            );
        }

        return {
            "x-device-token": deviceToken,
            "x-session-token": sessionToken
        };
    }

    async function waitForQueueSpot() {
        while (
            latestQueueCapacity &&
            latestQueueCapacity.pendingCount != null &&
            latestQueueCapacity.limit != null &&
            latestQueueCapacity.pendingCount >= latestQueueCapacity.limit
        ) {
            updateStatus(
                `Queue full (${latestQueueCapacity.pendingCount}/${latestQueueCapacity.limit}), waiting for a spot…`
            );
            await interruptibleSleep(1000);
            if (state.stopping) throw new Error("Stopped by user");
        }
    }

    function parseRetryAfter(value) {
        if (!value) return null;

        const seconds = Number(value);
        if (Number.isFinite(seconds) && seconds >= 0) {
            return seconds * 1000;
        }

        const timestamp = Date.parse(value);
        if (Number.isFinite(timestamp)) {
            return Math.max(0, timestamp - Date.now());
        }

        return null;
    }

    function withJitter(ms) {
        const jitter = ms * (0.85 + Math.random() * 0.3);
        return Math.round(jitter);
    }

    function createRequestController() {
        const controller = new AbortController();
        const timeout = setTimeout(() => {
            controller.abort(
                new DOMException("Request timed out", "TimeoutError")
            );
        }, CONFIG.requestTimeoutMs);

        state.requestControllers.add(controller);

        return {
            controller,
            cleanup() {
                clearTimeout(timeout);
                state.requestControllers.delete(controller);
            }
        };
    }

    function interruptibleSleep(ms) {
        return new Promise((resolve, reject) => {
            if (state.stopping) {
                reject(new Error("Stopped by user"));
                return;
            }

            let remaining = Math.max(0, ms);
            let startedAt = Date.now();
            let timer = null;

            const tick = () => {
                if (state.stopping) {
                    state.sleepCancel = null;
                    reject(new Error("Stopped by user"));
                    return;
                }

                if (state.paused) {
                    startedAt = Date.now();
                    timer = setTimeout(tick, 200);
                    return;
                }

                const elapsed = Date.now() - startedAt;
                remaining -= elapsed;

                if (remaining <= 0) {
                    state.sleepCancel = null;
                    resolve();
                    return;
                }

                startedAt = Date.now();
                timer = setTimeout(tick, Math.min(remaining, 250));
            };

            state.sleepCancel = () => {
                clearTimeout(timer);
                state.sleepCancel = null;
                reject(new Error("Stopped by user"));
            };

            timer = setTimeout(tick, Math.min(remaining, 250));
        });
    }

    async function waitWhilePaused() {
        while (state.paused && !state.stopping) {
            await interruptibleSleep(200);
        }
        if (state.stopping) throw new Error("Stopped by user");
    }

    function isStopError(error) {
        return (
            state.stopping ||
            error?.name === "AbortError" ||
            error?.message === "Stopped by user"
        );
    }

    function isAuthError(error) {
        return (
            error?.authFailed === true ||
            /authentication failed/i.test(String(error?.message || error))
        );
    }

    async function fetchJson(url, options, context, retries) {
        let lastError;

        for (let attempt = 0; attempt <= retries; attempt++) {
            await waitWhilePaused();
            const request = createRequestController();

            try {
                const response = await originalFetch(url, {
                    ...options,
                    signal: request.controller.signal
                });

                if (response.status === 404) {
                    return { __notFound: true };
                }

                if (response.status === 410) {
                    const body = await response.text().catch(() => "");
                    const error = new Error(
                        `${context}: HTTP 410 Gone${
                            body.trim() ? `: ${body.trim().slice(0, 300)}` : ""
                        }`
                    );
                    error.noRetry = true;
                    throw error;
                }

                if (response.status === 401 || response.status === 403) {
                    const error = new Error(
                        `${context}: authentication failed (${response.status}). Refresh Datacat and try again.`
                    );
                    error.authFailed = true;
                    error.noRetry = true;
                    throw error;
                }

                if (response.status === 429 || response.status >= 500) {
                    if (attempt >= retries) {
                        throw new Error(
                            `${context}: HTTP ${response.status} after ${attempt + 1} attempts`
                        );
                    }

                    const retryAfter = parseRetryAfter(
                        response.headers.get("retry-after")
                    );
                    const exponentialBackoff = Math.min(
                        CONFIG.maxBackoffMs,
                        CONFIG.fallbackRateLimitMs * 2 ** attempt
                    );
                    const waitMs = withJitter(
                        Math.min(
                            CONFIG.maxBackoffMs,
                            Math.max(retryAfter ?? 0, exponentialBackoff)
                        )
                    );

                    appendLog(
                        `${context}: HTTP ${response.status}; retry ${attempt + 1}/${retries} in ${formatDuration(waitMs)}.`,
                        "warning"
                    );
                    updateStatus(
                        `${context}: temporarily blocked; retrying in ${formatDuration(waitMs)}…`
                    );
                    await interruptibleSleep(waitMs);
                    continue;
                }

                if (!response.ok) {
                    const body = await response.text().catch(() => "");
                    const details = body.trim()
                        ? `: ${body.trim().slice(0, 300)}`
                        : "";
                    const error = new Error(
                        `${context}: HTTP ${response.status} ${response.statusText}${details}`
                    );
                    if (response.status >= 400 && response.status < 500) {
                        error.noRetry = true;
                    }
                    throw error;
                }

                const contentType =
                    response.headers.get("content-type") || "";
                if (!contentType.includes("application/json")) {
                    const body = await response.text();
                    throw new Error(
                        `${context}: expected JSON but received ${contentType || "an unknown content type"}: ${body.slice(0, 200)}`
                    );
                }

                return await response.json();
            } catch (error) {
                lastError = error;

                if (error?.noRetry) throw error;
                if (isStopError(error)) throw new Error("Stopped by user");

                if (attempt >= retries) throw error;

                const waitMs = withJitter(
                    Math.min(CONFIG.maxBackoffMs, 2000 * 2 ** attempt)
                );
                appendLog(
                    `${context}: ${String(error)}; retry ${attempt + 1}/${retries} in ${formatDuration(waitMs)}.`,
                    "warning"
                );
                await interruptibleSleep(waitMs);
            } finally {
                request.cleanup();
            }
        }

        throw lastError || new Error(`${context}: request failed`);
    }

    async function characterExists(characterId, retries) {
        const url =
            "https://datacat.run/api/characters/recent-public/" +
            encodeURIComponent(characterId) +
            "?view=modal&sourceKind=janitor";

        try {
            const data = await fetchJson(
                url,
                {
                    method: "GET",
                    credentials: "include",
                    headers: {
                        accept: "application/json",
                        "cache-control": "no-cache",
                        pragma: "no-cache",
                        ...getAuthHeaders()
                    }
                },
                `Checking ${characterId}`,
                retries
            );

            if (data?.__notFound || data == null) return false;

            return data?.modal_detail === true && data?.success === true;
        } catch (error) {
            if (
                error?.noRetry &&
                String(error.message).includes("ERROR_4152_DELETED")
            ) {
                return "deleted";
            }
            throw new Error(
                `Existence check failed for ${characterId}: ${error.message}`
            );
        }
    }

    async function retrieveCharacter(characterId, options) {
        await waitForQueueSpot();
        const characterUrl =
            "https://janitorai.com/characters/" +
            encodeURIComponent(characterId);

        return fetchJson(
            "https://datacat.run/api/character/retrieval-v2",
            {
                method: "POST",
                credentials: "include",
                headers: {
                    accept: "application/json",
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    pragma: "no-cache",
                    "x-request-id": crypto.randomUUID
                        ? crypto.randomUUID()
                        : `${characterId}-${Date.now()}`,
                    ...getAuthHeaders()
                },
                body: JSON.stringify({
                    url: characterUrl,
                    openLoginIfNoSession: true,
                    appearOnPublicFeed: options.appearOnPublicFeed,
                    publicFeedVisibilityIntent: options.appearOnPublicFeed,
                    useSeparateWorkerServer: options.useSeparateWorker,
                    inlinePostExtractCreatorProfile: true,
                    idempotencyKey: characterId,
                    extractSourceMode: "core_plus_janny",
                    alwaysReextract: options.alwaysReextract
                })
            },
            `Retrieving ${characterId}`,
            options.retries
        );
    }

    function extractCharacterId(value) {
        let input = String(value).trim();

        if (!input || input.startsWith("#")) return null;

        input = input.replace(/^[\"'(<\[]+|[\"')>\],;]+$/g, "");

        try {
            const url = new URL(input);
            const parts = url.pathname.split("/").filter(Boolean);
            const index = parts.findIndex(
                (part) => part.toLowerCase() === "characters"
            );
            if (index !== -1 && parts[index + 1]) {
                try {
                    return decodeURIComponent(parts[index + 1]);
                } catch {
                    return null;
                }
            }
        } catch {
            // fall through
        }

        const pathMatch = input.match(
            /(?:https?:\/\/)?(?:www\.)?janitorai\.com\/characters\/([^/?#\s]+)/i
        );
        if (pathMatch) {
            try {
                return decodeURIComponent(pathMatch[1]);
            } catch {
                return null;
            }
        }

        // Prefer UUID-looking tokens; also accept bare slug/id tokens
        const uuidMatch = input.match(
            /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
        );
        if (uuidMatch) return uuidMatch[0].toLowerCase();

        return /^[a-zA-Z0-9_-]{6,}$/.test(input) ? input : null;
    }

    function parseCharacterIds(text) {
        const ids = [];
        const invalid = [];
        const seen = new Set();

        for (const line of text.split(/\r?\n/)) {
            if (line.trim().startsWith("#")) continue;

            for (const token of line.split(/[\s,]+/)) {
                if (!token) continue;

                const id = extractCharacterId(token);
                if (!id) {
                    invalid.push(token);
                    continue;
                }
                if (!seen.has(id)) {
                    seen.add(id);
                    ids.push(id);
                }
            }
        }

        return { ids, invalid };
    }

    function formatDuration(ms) {
        if (ms < 1000) return `${Math.round(ms)} ms`;
        const seconds = Math.ceil(ms / 1000);
        if (seconds < 60) return `${seconds}s`;
        const minutes = Math.floor(seconds / 60);
        const remainder = seconds % 60;
        return `${minutes}m ${remainder}s`;
    }

    function formatEta(done, total, startedAtMs) {
        if (!startedAtMs || done <= 0 || done >= total) return "";
        const elapsed = Date.now() - startedAtMs;
        const perItem = elapsed / done;
        const remaining = Math.round(perItem * (total - done));
        return ` · ETA ${formatDuration(remaining)}`;
    }

    function timestamp() {
        return new Date().toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit"
        });
    }

    const settings = loadSettings();

    const panel = document.createElement("section");
    panel.id = "datacat-bulk-panel";
    panel.innerHTML = `
        <header class="datacat-panel-header" title="Drag to move">
            <div>
                <strong>Bulk character retrieval</strong>
                <small id="datacat-version">v${SCRIPT_VERSION}</small>
            </div>
            <button type="button" id="datacat-toggle-panel" title="Minimise">−</button>
        </header>

        <div id="datacat-panel-content">
            <textarea
                id="datacat-links-input"
                spellcheck="false"
                placeholder="Paste JanitorAI links or character IDs. Spaces, commas and new lines are accepted."
            ></textarea>

            <div class="datacat-row">
                <label>
                    Delay
                    <input id="datacat-delay-input" type="number" min="0" step="0.5">
                    s
                </label>

                <label>
                    Retries
                    <input id="datacat-retries-input" type="number" min="0" max="10" step="1">
                </label>

                <span id="datacat-queue-count">Queue: ? / ?</span>
                <span id="datacat-count">0 valid IDs</span>
            </div>

            <details class="datacat-advanced">
                <summary>Options</summary>

                <label>
                    <input id="datacat-skip-existing" type="checkbox">
                    Skip characters already present
                </label>

                <label>
                    <input id="datacat-always-reextract" type="checkbox">
                    Force re-extraction
                </label>

                <label>
                    <input id="datacat-public-feed" type="checkbox">
                    Add retrieved characters to the public feed
                </label>

                <label>
                    <input id="datacat-separate-worker" type="checkbox">
                    Use separate worker server
                </label>
            </details>

            <div class="datacat-progress-wrap">
                <progress id="datacat-progress" max="1" value="0"></progress>
                <span id="datacat-progress-label">0 / 0</span>
            </div>

            <div class="datacat-actions">
                <button type="button" id="datacat-start-button">Start</button>
                <button type="button" id="datacat-pause-button" disabled>Pause</button>
                <button type="button" id="datacat-stop-button" disabled>Stop</button>
                <button type="button" id="datacat-clear-button">Clear</button>
            </div>

            <div class="datacat-actions datacat-secondary-actions">
                <button type="button" id="datacat-retry-failed-button" disabled>Retry failed</button>
                <button type="button" id="datacat-copy-failed-button" disabled>Copy failed</button>
                <button type="button" id="datacat-export-button" disabled>Export JSON</button>
                <button type="button" id="datacat-resume-button" disabled>Resume job</button>
            </div>

            <div id="datacat-status" role="status">Ready.</div>
            <div id="datacat-stats"></div>
            <div id="datacat-log"></div>
        </div>
    `;

    const style = document.createElement("style");
    style.textContent = `
        #datacat-bulk-panel {
            position: fixed;
            z-index: 2147483647;
            width: min(470px, calc(100vw - 24px));
            max-height: calc(100vh - 24px);
            padding: 12px;
            overflow: auto;
            border: 1px solid rgba(255,255,255,.18);
            border-radius: 12px;
            background: rgba(18,18,22,.97);
            color: #f5f5f5;
            box-shadow: 0 12px 40px rgba(0,0,0,.45);
            font: 14px/1.4 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
            backdrop-filter: blur(12px);
        }
        #datacat-bulk-panel * { box-sizing: border-box; }
        .datacat-panel-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            margin-bottom: 10px;
            cursor: move;
            user-select: none;
        }
        .datacat-panel-header > div {
            display: flex;
            align-items: baseline;
            gap: 7px;
        }
        #datacat-version { color: #9797a3; font-size: 10px; }
        #datacat-toggle-panel {
            width: 30px; height: 30px; padding: 0; border: 0;
            border-radius: 7px; background: #35353d; color: #fff;
            cursor: pointer; font-size: 20px;
        }
        #datacat-links-input {
            display: block; width: 100%; min-height: 180px; max-height: 48vh;
            resize: vertical; padding: 10px; border: 1px solid #454550;
            border-radius: 8px; outline: none; background: #111116;
            color: #f5f5f5;
            font: 12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;
        }
        #datacat-links-input:focus { border-color: #8d7dff; }
        .datacat-row {
            display: flex; align-items: center; justify-content: space-between;
            flex-wrap: wrap; gap: 8px; margin-top: 10px; color: #cfcfd6;
        }
        .datacat-row label { display: flex; align-items: center; gap: 5px; }
        .datacat-row input[type="number"] {
            width: 58px; padding: 5px 7px; border: 1px solid #454550;
            border-radius: 6px; background: #111116; color: #fff;
        }
        .datacat-advanced {
            margin-top: 9px; padding: 7px 9px; border-radius: 7px;
            background: #202027; color: #cfcfd6;
        }
        .datacat-advanced summary { cursor: pointer; }
        .datacat-advanced label { display: block; margin-top: 6px; cursor: pointer; }
        .datacat-progress-wrap {
            display: flex; align-items: center; gap: 8px; margin-top: 10px;
        }
        #datacat-progress { width: 100%; height: 12px; }
        #datacat-progress-label {
            min-width: 48px; color: #bcbcc5; font-size: 11px; text-align: right;
        }
        .datacat-actions { display: flex; gap: 7px; margin-top: 9px; }
        .datacat-actions button {
            flex: 1; min-width: 0; padding: 8px 7px; border: 0;
            border-radius: 7px; cursor: pointer; font-weight: 600;
        }
        #datacat-start-button { background: #705cff; color: #fff; }
        #datacat-pause-button { background: #b27c27; color: #fff; }
        #datacat-stop-button { background: #b94141; color: #fff; }
        #datacat-clear-button,
        .datacat-secondary-actions button { background: #35353d; color: #fff; }
        .datacat-actions button:disabled { cursor: not-allowed; opacity: .42; }
        .datacat-secondary-actions button { padding: 6px; font-size: 11px; }
        #datacat-status {
            margin-top: 10px; padding: 8px; border-radius: 7px;
            background: #292932; color: #dddde5; overflow-wrap: anywhere;
        }
        #datacat-stats {
            min-height: 17px; margin-top: 6px; color: #aaaab5; font-size: 11px;
        }
        #datacat-log {
            display: none; max-height: 160px; margin-top: 8px; padding: 8px;
            overflow: auto; border-radius: 7px; background: #111116;
            color: #bcbcc5;
            font: 11px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;
            white-space: pre-wrap; overflow-wrap: anywhere;
        }
        #datacat-log .success { color: #7fd49a; }
        #datacat-log .warning { color: #e2b96c; }
        #datacat-log .error { color: #eb8888; }
        #datacat-log .muted { color: #92929c; }
        #datacat-bulk-panel.minimised { width: 285px; overflow: hidden; }
        #datacat-bulk-panel.minimised #datacat-panel-content { display: none; }
        #datacat-bulk-panel.minimised .datacat-panel-header { margin-bottom: 0; }
        @media (max-width: 520px) {
            #datacat-bulk-panel {
                right: 8px !important; left: 8px !important;
                bottom: 8px !important; top: auto !important; width: auto;
            }
        }
    `;

    function mountPanel() {
        (document.head || document.documentElement).appendChild(style);
        document.body.appendChild(panel);
    }

    if (document.body) {
        mountPanel();
    } else {
        document.addEventListener("DOMContentLoaded", mountPanel, { once: true });
    }

    elements = {
        input: $("#datacat-links-input", panel),
        delay: $("#datacat-delay-input", panel),
        retries: $("#datacat-retries-input", panel),
        count: $("#datacat-count", panel),
        skipExisting: $("#datacat-skip-existing", panel),
        alwaysReextract: $("#datacat-always-reextract", panel),
        publicFeed: $("#datacat-public-feed", panel),
        separateWorker: $("#datacat-separate-worker", panel),
        progress: $("#datacat-progress", panel),
        progressLabel: $("#datacat-progress-label", panel),
        start: $("#datacat-start-button", panel),
        pause: $("#datacat-pause-button", panel),
        stop: $("#datacat-stop-button", panel),
        clear: $("#datacat-clear-button", panel),
        retryFailed: $("#datacat-retry-failed-button", panel),
        copyFailed: $("#datacat-copy-failed-button", panel),
        export: $("#datacat-export-button", panel),
        resume: $("#datacat-resume-button", panel),
        toggle: $("#datacat-toggle-panel", panel),
        status: $("#datacat-status", panel),
        stats: $("#datacat-stats", panel),
        log: $("#datacat-log", panel),
        header: $(".datacat-panel-header", panel),
        queueCount: $("#datacat-queue-count", panel)
    };

    pageWindow.addEventListener("message", (event) => {
        if (
            !JANITOR_ORIGINS.has(event.origin) ||
            event.data?.type !== "datacat:janitor-import" ||
            !Array.isArray(event.data.ids)
        ) {
            return;
        }

        const result = importJanitorIds(event.data.ids);
        event.source?.postMessage(
            { type: "datacat:janitor-imported", ...result },
            event.origin
        );
    });

    function updateStatus(message) {
        elements.status.textContent = message;
        console.log("[Datacat bulk]", message);
    }

    function appendLog(message, type = "muted") {
        elements.log.style.display = "block";
        const line = document.createElement("div");
        line.className = type;
        line.textContent = `[${timestamp()}] ${message}`;
        elements.log.appendChild(line);

        while (elements.log.childElementCount > CONFIG.maxLogLines) {
            elements.log.removeChild(elements.log.firstChild);
        }
        elements.log.scrollTop = elements.log.scrollHeight;
    }

    function getFailedIds() {
        return state.results
            .filter((result) => result.status === "failed")
            .map((result) => result.id);
    }

    function countByStatus() {
        const counts = { retrieved: 0, skipped: 0, failed: 0 };
        for (const result of state.results) {
            if (result.status in counts) counts[result.status]++;
        }
        return counts;
    }

    function updateResultActions() {
        const hasResults = state.results.length > 0;
        const hasFailures = getFailedIds().length > 0;
        const job = loadJob();
        const hasResumable =
            !state.running &&
            job &&
            Array.isArray(job.pendingIds) &&
            job.currentIndex < job.pendingIds.length;

        elements.export.disabled = !hasResults;
        elements.copyFailed.disabled = !hasFailures;
        elements.retryFailed.disabled = !hasFailures || state.running;
        elements.resume.disabled = !hasResumable;
    }

    function updateStats() {
        const counts = countByStatus();
        elements.stats.textContent =
            `Retrieved: ${counts.retrieved} · Skipped: ${counts.skipped} · Failed: ${counts.failed}`;
        updateResultActions();
    }

    function updateProgress(current, total) {
        elements.progress.max = Math.max(1, total);
        elements.progress.value = Math.min(current, total);
        elements.progressLabel.textContent = `${current} / ${total}`;
    }

    function updateCount() {
        const { ids, invalid } = parseCharacterIds(elements.input.value);
        elements.count.textContent =
            `${ids.length} valid ID${ids.length === 1 ? "" : "s"}` +
            (invalid.length ? ` · ${invalid.length} invalid` : "");
        saveSettingsDebounced({ input: elements.input.value });
    }

    // Assigning textarea.value does not emit an input event. Keep those
    // programmatic queue changes (imports and completed work) visible to the
    // other Datacat tabs just like a user edit.
    function setInputValue(value, { mirror = false } = {}) {
        const changed = value !== elements.input.value;
        if (changed) {
            elements.input.value = value;
            updateCount();
        }
        if (mirror) {
            clearTimeout(inputSyncTimer);
            GM_setValue(datacatInputKey, value);
        }
        return changed;
    }

    function terminalQueueIds(results = state.results) {
        return new Set(
            results
                .filter(
                    (result) =>
                        result.status === "retrieved" || result.status === "skipped"
                )
                .map((result) => result.id)
        );
    }

    function remainingQueueIds(ids, results = state.results) {
        const terminalIds = terminalQueueIds(results);
        return ids.filter((id) => !terminalIds.has(id));
    }

    function excludeKnownExistingIds(ids) {
        if (!elements.skipExisting.checked || elements.alwaysReextract.checked) {
            return ids;
        }
        return ids.filter((id) => !existingIdCache.has(id));
    }

    function importJanitorIds(rawIds) {
        if (state.running) {
            updateStatus("Datacat is busy; finish or stop the current queue before importing.");
            return { count: 0, busy: true };
        }

        const imported = parseCharacterIds(
            rawIds.filter((id) => typeof id === "string").slice(0, 10000).join("\n")
        ).ids;
        const newIds = excludeKnownExistingIds(imported);
        const existing = parseCharacterIds(elements.input.value).ids;
        const ids = [...new Set([...existing, ...newIds])];
        setInputValue(ids.join("\n"), { mirror: true });
        elements.input.focus();
        updateStatus(
            `Imported ${newIds.length} Janitor ID${newIds.length === 1 ? "" : "s"}; ${ids.length} ready to retrieve.`
        );
        return { count: newIds.length, total: ids.length };
    }

    function syncJanitorIdsIntoInput() {
        const imported = excludeKnownExistingIds(activeJanitorIds());
        if (!imported.length) return;

        if (state.running) {
            const have = new Set(state.pendingIds);
            const added = imported.filter((id) => !have.has(id));
            if (!added.length) return;
            state.pendingIds.push(...added);
            setInputValue(remainingQueueIds(state.pendingIds).join("\n"), {
                mirror: true
            });
            updateProgress(state.results.length, state.pendingIds.length);
            persistJobProgress();
            state.enqueue?.();
            updateStatus(
                `Queued ${added.length} more ID${added.length === 1 ? "" : "s"} into the running job.`
            );
            return;
        }

        const existing = parseCharacterIds(elements.input.value).ids;
        const ids = [...new Set([...existing, ...imported])];
        if (ids.length === existing.length) return;
        setInputValue(ids.join("\n"), { mirror: true });
        updateStatus(
            `Synced ${imported.length} ID${imported.length === 1 ? "" : "s"} from active Janitor tab${imported.length === 1 ? "" : "s"}.`
        );
    }

    function syncSharedJob(job) {
        if (state.running || !job || !Array.isArray(job.pendingIds)) return;
        state.pendingIds = [...job.pendingIds];
        state.currentIndex = Number.isFinite(job.currentIndex) ? job.currentIndex : 0;
        state.results = Array.isArray(job.results) ? [...job.results] : [];
        setInputValue(
            remainingQueueIds(state.pendingIds, state.results).join("\n")
        );
        applyJobOptions(job.options);
        updateProgress(state.results.length, state.pendingIds.length);
        updateStats();
        if (state.currentIndex < state.pendingIds.length) {
            updateStatus(
                `Another Datacat tab is processing ${state.currentIndex}/${state.pendingIds.length}.`
            );
        }
    }

    function importJanitorIdsFromHash() {
        const params = new URLSearchParams(location.hash.slice(1));
        const encodedIds = params.get("janitorIds");
        if (!encodedIds) return;

        const result = importJanitorIds(encodedIds.split(","));
        history.replaceState(null, "", `${location.pathname}${location.search}`);
        return result;
    }

    function readOptions() {
        const delaySeconds = Number(elements.delay.value);
        const retries = Number(elements.retries.value);

        return {
            delayMs:
                Number.isFinite(delaySeconds) && delaySeconds >= 0
                    ? delaySeconds * 1000
                    : CONFIG.defaultDelayMs,
            retries:
                Number.isFinite(retries) && retries >= 0
                    ? Math.min(10, Math.floor(retries))
                    : CONFIG.defaultRetries,
            skipExisting: elements.skipExisting.checked,
            alwaysReextract: elements.alwaysReextract.checked,
            appearOnPublicFeed: elements.publicFeed.checked,
            useSeparateWorker: elements.separateWorker.checked
        };
    }

    function persistOptions() {
        saveSettings({
            delaySeconds: elements.delay.value,
            retries: elements.retries.value,
            skipExisting: elements.skipExisting.checked,
            alwaysReextract: elements.alwaysReextract.checked,
            publicFeed: elements.publicFeed.checked,
            separateWorker: elements.separateWorker.checked
        });
    }

    function applyJobOptions(options) {
        if (!options || typeof options !== "object") return;

        if (Number.isFinite(options.delayMs) && options.delayMs >= 0) {
            elements.delay.value = options.delayMs / 1000;
        }
        if (Number.isFinite(options.retries) && options.retries >= 0) {
            elements.retries.value = Math.min(10, Math.floor(options.retries));
        }
        for (const [key, element] of [
            ["skipExisting", elements.skipExisting],
            ["alwaysReextract", elements.alwaysReextract],
            ["appearOnPublicFeed", elements.publicFeed],
            ["useSeparateWorker", elements.separateWorker]
        ]) {
            if (typeof options[key] === "boolean") element.checked = options[key];
        }
        persistOptions();
    }

    function setRunningUi(running) {
        elements.start.disabled = running;
        elements.stop.disabled = !running;
        elements.pause.disabled = !running;
        elements.clear.disabled = running;
        elements.input.disabled = running;
        elements.delay.disabled = running;
        elements.retries.disabled = running;
        elements.skipExisting.disabled = running;
        elements.alwaysReextract.disabled = running;
        elements.publicFeed.disabled = running;
        elements.separateWorker.disabled = running;
        updateResultActions();
    }

    async function processIds(sourceIds, { resumeFrom = 0, priorResults = [] } = {}) {
        if (!(await acquireWorkerLease())) {
            const lease = getWorkerLease();
            updateStatus(
                lease?.expiresAt > Date.now()
                    ? "Another Datacat tab is already retrieving this shared queue."
                    : "Could not acquire the shared retrieval worker; try again."
            );
            return;
        }
        startWorkerHeartbeat();
        const options = readOptions();

        state.running = true;
        state.paused = false;
        state.stopping = false;
        state.results = [...priorResults];
        state.pendingIds = [...sourceIds];
        // Live queue: pushing to state.pendingIds (e.g. Janitor sends mid-run)
        // extends this run because the loop and existence checks read it directly.
        const ids = state.pendingIds;
        state.currentIndex = resumeFrom;
        state.startedAtMs = Date.now();
        persistJobProgress();

        if (resumeFrom === 0) {
            elements.log.textContent = "";
            elements.log.style.display = "none";
        }

        elements.pause.textContent = "Pause";
        updateProgress(state.results.length, ids.length);
        updateStats();
        setRunningUi(true);

        appendLog(
            resumeFrom > 0
                ? `Resumed queue at ${resumeFrom + 1}/${ids.length}.`
                : `Started queue with ${ids.length} character(s).`
        );

        const shouldCheckExisting =
            options.skipExisting && !options.alwaysReextract;
        const existingStatuses = new Map();
        const completedIds = new Set(state.results.map((result) => result.id));
        const startedIds = new Set();
        let nextExistenceCheckIndex = resumeFrom;
        let activeExistenceChecks = 0;
        let queueRefreshTimer = null;
        const refreshQueueWithoutCompleted = () => {
            const { ids: queuedIds } = parseCharacterIds(elements.input.value);
            const terminalIds = terminalQueueIds();
            const remainingIds = queuedIds.filter((id) => {
                const status = existingStatuses.get(id);
                return (
                    !terminalIds.has(id) &&
                    status !== true &&
                    status !== "deleted"
                );
            });
            if (remainingIds.length === queuedIds.length) return;
            setInputValue(remainingIds.join("\n"), { mirror: true });
        };
        const updateQueueProgress = () => {
            updateProgress(
                Math.max(
                    state.results.length,
                    state.activeIndex === null ? 0 : state.activeIndex + 1
                ),
                ids.length
            );
        };
        const scheduleQueueRefresh = () => {
            if (queueRefreshTimer !== null) return;
            queueRefreshTimer = setTimeout(() => {
                queueRefreshTimer = null;
                refreshQueueWithoutCompleted();
            }, 100);
        };
        const fillExistenceCheckQueue = () => {
            while (
                shouldCheckExisting &&
                !state.stopping &&
                activeExistenceChecks < CONFIG.existenceCheckConcurrency &&
                nextExistenceCheckIndex < ids.length
            ) {
                const index = nextExistenceCheckIndex++;
                activeExistenceChecks++;
                const id = ids[index];
                const cachedExisting = existingIdCache.has(id);
                const check = (cachedExisting
                    ? Promise.resolve(true)
                    : characterExists(id, options.retries)
                )
                    .then(
                        (value) => {
                            existingStatuses.set(id, value);
                            if (value === true && !cachedExisting) {
                                cacheExistingId(id);
                            }
                            if (value !== true && value !== "deleted") {
                                return { value };
                            }
                            scheduleQueueRefresh();
                            if (!startedIds.has(id) && !completedIds.has(id)) {
                                state.results.push({
                                    id,
                                    status: "skipped",
                                    reason:
                                        value === "deleted"
                                            ? "deleted by creator"
                                            : "already exists",
                                    durationMs: 0
                                });
                                completedIds.add(id);
                                updateStats();
                                updateQueueProgress();
                                persistJobProgress();
                            }
                            return { value };
                        },
                        (error) => ({ error })
                    )
                    .finally(() => {
                        activeExistenceChecks--;
                        fillExistenceCheckQueue();
                    });
            }
        };

        state.enqueue = fillExistenceCheckQueue;
        fillExistenceCheckQueue();

        try {
            let index = resumeFrom;
            while (true) {
                // Keep the worker alive briefly after draining the queue. This
                // closes the race where a Janitor send arrives at the exact
                // moment the final item completes.
                if (index >= ids.length) {
                    const drainedLength = ids.length;
                    await interruptibleSleep(CONFIG.liveQueueSettleMs);
                    if (ids.length <= drainedLength) break;
                    continue;
                }
                await waitWhilePaused();
                if (!renewWorkerLease()) {
                    throw new Error("Retrieval worker lease was taken by another tab");
                }
                if (state.stopping) throw new Error("Stopped by user");

                const id = ids[index];
                const startedAt = performance.now();

                if (completedIds.has(id)) {
                    state.currentIndex = index + 1;
                    persistJobProgress();
                    index++;
                    continue;
                }
                state.activeIndex = index;
                startedIds.add(id);

                updateQueueProgress();
                const eta = formatEta(index - resumeFrom, ids.length - resumeFrom, state.startedAtMs);
                updateStatus(
                    `[${index + 1}/${ids.length}] Processing ${id}…${eta}`
                );

                try {
                    if (shouldCheckExisting) {
                        const existenceStatus = existingStatuses.get(id);

                        if (existenceStatus === "deleted") {
                            state.results.push({
                                id,
                                status: "skipped",
                                reason: "deleted by creator",
                                durationMs: Math.round(
                                    performance.now() - startedAt
                                )
                            });
                            completedIds.add(id);
                            appendLog(
                                `Skipped ${id}: deleted by creator.`,
                                "muted"
                            );
                            updateStats();
                            state.activeIndex = null;
                            updateQueueProgress();
                            state.currentIndex = index + 1;
                            persistJobProgress();
                            index++;
                            continue;
                        }

                        if (existenceStatus === true) {
                            state.results.push({
                                id,
                                status: "skipped",
                                reason: "already exists",
                                durationMs: Math.round(
                                    performance.now() - startedAt
                                )
                            });
                            completedIds.add(id);
                            appendLog(`Skipped ${id}: already exists.`, "muted");
                            updateStats();
                            state.activeIndex = null;
                            updateQueueProgress();
                            state.currentIndex = index + 1;
                            persistJobProgress();
                            index++;
                            continue;
                        }
                    }

                    updateStatus(
                        `[${index + 1}/${ids.length}] Retrieving ${id}…`
                    );
                    const data = await retrieveCharacter(id, options);

                    state.results.push({
                        id,
                        status: "retrieved",
                        durationMs: Math.round(performance.now() - startedAt),
                        data
                    });
                    completedIds.add(id);
                    appendLog(`Retrieved ${id}.`, "success");
                    scheduleQueueRefresh();
                } catch (error) {
                    if (error.message === "Stopped by user") throw error;

                    if (isAuthError(error)) {
                        state.results.push({
                            id,
                            status: "failed",
                            durationMs: Math.round(
                                performance.now() - startedAt
                            ),
                            error: String(error)
                        });
                        completedIds.add(id);
                        appendLog(`Auth failure on ${id}: ${String(error)}`, "error");
                        state.currentIndex = index + 1;
                        updateStats();
                        state.activeIndex = null;
                        updateQueueProgress();
                        throw error;
                    }

                    state.results.push({
                        id,
                        status: "failed",
                        durationMs: Math.round(performance.now() - startedAt),
                        error: String(error)
                    });
                    completedIds.add(id);
                    appendLog(`Failed ${id}: ${String(error)}`, "error");
                    console.error(`[Datacat bulk] Failed ${id}`, error);
                }

                updateStats();
                state.activeIndex = null;
                updateQueueProgress();
                state.currentIndex = index + 1;
                persistJobProgress();

                if (index < ids.length - 1 && !state.stopping) {
                    updateStatus(
                        `Waiting ${formatDuration(options.delayMs)} before the next character…`
                    );
                    await interruptibleSleep(options.delayMs);
                }
                index++;
            }

            const counts = countByStatus();
            updateStatus(
                `Finished. Retrieved: ${counts.retrieved}, skipped: ${counts.skipped}, failed: ${counts.failed}.`
            );
            saveJob(null);
        } catch (error) {
            if (error.message === "Stopped by user") {
                updateStatus(
                    `Stopped after ${state.results.length} of ${ids.length} characters.`
                );
                appendLog("Queue stopped by user.", "warning");
                persistJobProgress();
            } else if (isAuthError(error)) {
                updateStatus(
                    `Stopped: authentication failed. Refresh Datacat and resume.`
                );
                appendLog(`Queue aborted: ${String(error)}`, "error");
                persistJobProgress();
            } else {
                updateStatus(`Unexpected error: ${String(error)}`);
                appendLog(`Unexpected error: ${String(error)}`, "error");
                console.error(error);
                persistJobProgress();
            }
        } finally {
            state.running = false;
            state.paused = false;
            state.stopping = false;
            for (const controller of state.requestControllers) controller.abort();
            state.requestControllers.clear();
            clearTimeout(queueRefreshTimer);
            refreshQueueWithoutCompleted();
            state.sleepCancel = null;
            state.activeIndex = null;
            state.startedAtMs = null;
            state.enqueue = null;
            releaseWorkerLease();
            setRunningUi(false);
            elements.pause.textContent = "Pause";
            updateStats();
        }
    }

    function stopImmediately() {
        if (!state.running) return;
        state.stopping = true;
        state.paused = false;
        elements.stop.disabled = true;
        elements.pause.disabled = true;
        updateStatus("Stopping…");
        for (const controller of state.requestControllers) controller.abort();
        state.sleepCancel?.();
    }

    function downloadResults() {
        const counts = countByStatus();
        const payload = {
            exportedAt: new Date().toISOString(),
            source: location.href,
            scriptVersion: SCRIPT_VERSION,
            totals: {
                submitted: state.pendingIds.length,
                completed: state.results.length,
                ...counts
            },
            results: state.results
        };

        const blob = new Blob([JSON.stringify(payload, null, 2)], {
            type: "application/json"
        });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `datacat-results-${new Date()
            .toISOString()
            .replace(/[:.]/g, "-")}.json`;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    async function copyFailedIds() {
        const failed = getFailedIds();
        if (!failed.length) return;

        const text = failed.join("\n");
        try {
            await navigator.clipboard.writeText(text);
        } catch {
            const ta = document.createElement("textarea");
            ta.value = text;
            ta.style.cssText = "position:fixed;left:-9999px;top:0";
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("copy");
            ta.remove();
        }
        updateStatus(
            `Copied ${failed.length} failed ID${failed.length === 1 ? "" : "s"}.`
        );
    }

    function applySavedSettings() {
        elements.input.value = settings.input || "";
        elements.delay.value =
            settings.delaySeconds ?? CONFIG.defaultDelayMs / 1000;
        elements.retries.value = settings.retries ?? CONFIG.defaultRetries;
        elements.skipExisting.checked = settings.skipExisting ?? true;
        elements.alwaysReextract.checked = settings.alwaysReextract ?? false;
        elements.publicFeed.checked = settings.publicFeed ?? true;
        elements.separateWorker.checked = settings.separateWorker ?? true;

        if (settings.minimised) {
            panel.classList.add("minimised");
            elements.toggle.textContent = "+";
            elements.toggle.title = "Expand";
        }

        if (Number.isFinite(settings.left) && Number.isFinite(settings.top)) {
            panel.style.left = `${Math.max(0, settings.left)}px`;
            panel.style.top = `${Math.max(0, settings.top)}px`;
        } else {
            panel.style.right = "20px";
            panel.style.bottom = "20px";
        }

        updateCount();
        updateStats();

        const job = loadJob();
        if (
            job &&
            Array.isArray(job.pendingIds) &&
            job.currentIndex < job.pendingIds.length
        ) {
            updateStatus(
                `Incomplete job found (${job.currentIndex}/${job.pendingIds.length}). Click Resume job.`
            );
        }
    }

    function clampPanelToViewport(panel) {
        const rect = panel.getBoundingClientRect();
        const left = Math.min(
            Math.max(0, pageWindow.innerWidth - rect.width),
            Math.max(0, rect.left)
        );
        const top = Math.min(
            Math.max(0, pageWindow.innerHeight - rect.height),
            Math.max(0, rect.top)
        );

        if (
            Math.round(rect.left) === Math.round(left) &&
            Math.round(rect.top) === Math.round(top)
        ) {
            return null;
        }

        panel.style.right = "auto";
        panel.style.bottom = "auto";
        panel.style.left = `${Math.round(left)}px`;
        panel.style.top = `${Math.round(top)}px`;
        return { left: Math.round(left), top: Math.round(top) };
    }

    function enableDragging(panel, header, onDrop) {
        let drag = null;

        const onMove = (event) => {
            if (!drag) return;
            const maxLeft = Math.max(0, pageWindow.innerWidth - panel.offsetWidth);
            const maxTop = Math.max(0, pageWindow.innerHeight - panel.offsetHeight);
            const left = Math.min(
                maxLeft,
                Math.max(0, event.clientX - drag.offsetX)
            );
            const top = Math.min(
                maxTop,
                Math.max(0, event.clientY - drag.offsetY)
            );
            panel.style.left = `${left}px`;
            panel.style.top = `${top}px`;
        };

        const onUp = (event) => {
            if (!drag) return;
            drag = null;
            try {
                header.releasePointerCapture(event.pointerId);
            } catch {
                /* ignore */
            }
            const rect = panel.getBoundingClientRect();
            onDrop?.(rect);
        };

        header.addEventListener("pointerdown", (event) => {
            if (event.target.closest("button")) return;
            const rect = panel.getBoundingClientRect();
            drag = {
                offsetX: event.clientX - rect.left,
                offsetY: event.clientY - rect.top
            };
            panel.style.right = "auto";
            panel.style.bottom = "auto";
            header.setPointerCapture(event.pointerId);
        });

        header.addEventListener("pointermove", onMove);
        header.addEventListener("pointerup", onUp);
        header.addEventListener("pointercancel", onUp);
    }

    // Mirror the raw textarea across datacat tabs. Full-text sync means append
    // and remove both propagate for free — no per-op command protocol needed.
    // ponytail: last-writer-wins, two people typing at once can clobber; add a
    // per-line merge only if concurrent editing becomes a real workflow.
    let inputSyncTimer = null;
    elements.input.addEventListener("input", () => {
        updateCount();
        clearTimeout(inputSyncTimer);
        inputSyncTimer = setTimeout(() => {
            GM_setValue(datacatInputKey, elements.input.value);
        }, CONFIG.saveDebounceMs);
    });
    GM_addValueChangeListener(datacatInputKey, (_key, _old, value, remote) => {
        if (!remote || state.running) return;
        if (typeof value !== "string" || value === elements.input.value) return;
        setInputValue(value);
    });

    for (const element of [
        elements.delay,
        elements.retries,
        elements.skipExisting,
        elements.alwaysReextract,
        elements.publicFeed,
        elements.separateWorker
    ]) {
        element.addEventListener("change", persistOptions);
    }

    elements.start.addEventListener("click", () => {
        if (state.running) return;

        const { ids, invalid } = parseCharacterIds(elements.input.value);
        if (!ids.length) {
            updateStatus("No valid JanitorAI links or character IDs found.");
            return;
        }
        if (invalid.length) {
            appendLog(
                `Ignored ${invalid.length} invalid input${invalid.length === 1 ? "" : "s"}.`,
                "warning"
            );
        }
        processIds(ids);
    });

    elements.pause.addEventListener("click", () => {
        if (!state.running) return;
        state.paused = !state.paused;
        elements.pause.textContent = state.paused ? "Resume" : "Pause";
        updateStatus(state.paused ? "Paused." : "Resuming…");
        persistJobProgress();
    });

    elements.stop.addEventListener("click", stopImmediately);

    elements.clear.addEventListener("click", () => {
        if (state.running) return;
        state.results = [];
        state.pendingIds = [];
        state.currentIndex = 0;
        setInputValue("", { mirror: true });
        elements.log.textContent = "";
        elements.log.style.display = "none";
        saveJob(null);
        updateStatus("Ready.");
        updateProgress(0, 0);
        updateStats();
    });

    elements.retryFailed.addEventListener("click", () => {
        const failed = getFailedIds();
        if (!state.running && failed.length) {
            setInputValue(failed.join("\n"), { mirror: true });
            processIds(failed);
        }
    });

    elements.resume.addEventListener("click", () => {
        if (state.running) return;
        const job = loadJob();
        if (
            !job ||
            !Array.isArray(job.pendingIds) ||
            job.currentIndex >= job.pendingIds.length
        ) {
            updateStatus("No incomplete job to resume.");
            updateResultActions();
            return;
        }
        setInputValue(job.pendingIds.join("\n"));
        applyJobOptions(job.options);
        processIds(job.pendingIds, {
            resumeFrom: job.currentIndex,
            priorResults: Array.isArray(job.results) ? job.results : []
        });
    });

    elements.copyFailed.addEventListener("click", () => {
        copyFailedIds().catch((error) => {
            updateStatus(`Could not copy failed IDs: ${String(error)}`);
        });
    });

    elements.export.addEventListener("click", downloadResults);

    elements.toggle.addEventListener("click", () => {
        const minimised = panel.classList.toggle("minimised");
        elements.toggle.textContent = minimised ? "+" : "−";
        elements.toggle.title = minimised ? "Expand" : "Minimise";
        saveSettings({ minimised });
    });

    document.addEventListener("keydown", (event) => {
        if (
            event.ctrlKey &&
            event.key === "Enter" &&
            !state.running &&
            document.activeElement === elements.input
        ) {
            event.preventDefault();
            elements.start.click();
        }
        if (event.key === "Escape" && state.running) {
            stopImmediately();
        }
    });

    pageWindow.addEventListener("beforeunload", (event) => {
        if (existingCacheSaveTimer !== null) {
            clearTimeout(existingCacheSaveTimer);
            saveExistingIdCache();
        }
        if (!state.running) return;
        persistJobProgress();
        releaseWorkerLease();
        event.preventDefault();
        event.returnValue = "";
    });

    applySavedSettings();
    const initialPanelPosition = clampPanelToViewport(panel);
    if (initialPanelPosition) saveSettings(initialPanelPosition);
    importJanitorIdsFromHash();
    syncJanitorIdsIntoInput();

    GM_addValueChangeListener(janitorRevisionKey, (_key, _old, _value, remote) => {
        if (remote) syncJanitorIdsIntoInput();
    });
    GM_addValueChangeListener(sharedJobKey, (_key, _old, job, remote) => {
        if (remote) syncSharedJob(job);
    });
    GM_addValueChangeListener(workerLeaseKey, (_key, _old, lease, remote) => {
        if (remote && state.running && lease?.tabId && lease.tabId !== TAB_ID) {
            updateStatus("Another tab took the retrieval worker; stopping this tab.");
            stopImmediately();
        }
    });
    syncSharedJob(loadJob());

    pageWindow.addEventListener("hashchange", importJanitorIdsFromHash);
    enableDragging(panel, elements.header, (rect) => {
        saveSettings({
            left: Math.round(rect.left),
            top: Math.round(rect.top)
        });
    });
    pageWindow.addEventListener("resize", () => {
        const position = clampPanelToViewport(panel);
        if (position) saveSettings(position);
    });
})();
