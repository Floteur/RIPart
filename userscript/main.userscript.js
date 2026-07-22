// ==UserScript==
// @name         Datacat Bulk JanitorAI Character Retriever
// @namespace    https://greasyfork.org/users/1622561-flonz
// @version      1.6.0
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

    const SCRIPT_VERSION = "1.6.0";
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
    const janitorClearKey = `${SHARED_KEY_PREFIX}janitor-clear`;
    const workerLeaseKey = `${SHARED_KEY_PREFIX}worker-lease`;
    const workerCommandKey = `${SHARED_KEY_PREFIX}worker-command`;
    const sharedJobKey = `${SHARED_KEY_PREFIX}job`;
    const datacatInputKey = `${SHARED_KEY_PREFIX}datacat-input`;
    let tabRole = "unknown";
    let workerHeartbeatId = null;

    function formatCount(count, singular, plural = `${singular}s`) {
        return `${count} ${count === 1 ? singular : plural}`;
    }

    function setStatus(element, message, tone = "neutral") {
        element.textContent = message;
        element.dataset.tone = tone;
    }

    async function copyToClipboard(text) {
        if (!text) return;

        try {
            await navigator.clipboard.writeText(text);
        } catch {
            const fallback = document.createElement("textarea");
            fallback.value = text;
            fallback.setAttribute("readonly", "");
            fallback.style.cssText =
                "position:fixed;left:-9999px;top:0;opacity:0;pointer-events:none";
            document.body.appendChild(fallback);
            fallback.select();
            let copied = false;
            try {
                copied = document.execCommand("copy");
            } finally {
                fallback.remove();
            }
            if (!copied) throw new Error("Clipboard access was denied");
        }
    }

    function setPanelMinimised(panel, button, minimised) {
        panel.classList.toggle("minimised", minimised);
        button.textContent = minimised ? "+" : "−";
        button.title = minimised ? "Expand" : "Minimise";
        button.setAttribute("aria-label", button.title);
        button.setAttribute("aria-expanded", String(!minimised));
    }

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
        const clearedAt = GM_getValue(janitorClearKey, null)?.at || 0;
        const ids = new Set();
        for (const key of GM_listValues()) {
            if (!key.startsWith(`${SHARED_KEY_PREFIX}tab:`)) continue;
            const tab = GM_getValue(key, null);
            if (
                tab?.role !== "janitor" ||
                !Array.isArray(tab.ids) ||
                !Number.isFinite(tab.seenAt) ||
                now - tab.seenAt > TAB_TTL_MS ||
                (clearedAt > 0 &&
                    (!Number.isFinite(tab.idsUpdatedAt) ||
                        tab.idsUpdatedAt < clearedAt))
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
        touchTab({ ids: [...ids], idsUpdatedAt: Date.now() });
        GM_setValue(janitorRevisionKey, { id: TAB_ID, at: Date.now() });
    }

    function clearPublishedJanitorIds(clearedAt) {
        for (const key of GM_listValues()) {
            if (!key.startsWith(`${SHARED_KEY_PREFIX}tab:`)) continue;
            const tab = GM_getValue(key, null);
            if (tab?.role === "janitor") {
                GM_setValue(key, {
                    ...tab,
                    ids: [],
                    idsUpdatedAt: clearedAt,
                    seenAt: Date.now()
                });
            }
        }
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
    setInterval(() => {
        touchTab();
        const lease = getWorkerLease();
        if (
            state.running &&
            !isLocalWorker() &&
            (!lease || lease.expiresAt <= Date.now())
        ) {
            syncSharedJob(loadJob());
        }
    }, WORKER_HEARTBEAT_MS);

    function mountJanitorCollector() {
        const collectedIds = new Set();
        let scanIntervalId = null;
        let scanGeneration = 0;
        let lastScanAdded = 0;
        let savedPosition = null;

        try {
            const saved = JSON.parse(
                localStorage.getItem(JANITOR_COLLECTOR_STORAGE_KEY) || "{}"
            );
            const clearedAt = GM_getValue(janitorClearKey, null)?.at || 0;
            if (
                clearedAt === 0 ||
                (Number.isFinite(saved.updatedAt) && saved.updatedAt >= clearedAt)
            ) {
                for (const id of Array.isArray(saved.ids) ? saved.ids : []) {
                    if (typeof id !== "string") continue;
                    const uuid = uuidFromValue(id);
                    if (uuid === id.toLowerCase()) collectedIds.add(uuid);
                }
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
        panel.setAttribute("aria-labelledby", "janitor-uuid-title");
        panel.innerHTML = `
            <header id="janitor-uuid-header" title="Drag to move">
                <div class="janitor-title-group">
                    <span class="janitor-app-mark" aria-hidden="true">J</span>
                    <div>
                        <strong id="janitor-uuid-title">Card collector</strong>
                        <small>JanitorAI · v${SCRIPT_VERSION}</small>
                    </div>
                </div>
                <button type="button" id="janitor-uuid-toggle" title="Minimise"
                    aria-label="Minimise" aria-controls="janitor-uuid-content" aria-expanded="true">−</button>
            </header>
            <div id="janitor-uuid-content">
                <div id="janitor-uuid-status" role="status" aria-live="polite" data-tone="neutral">Scanning this page…</div>
                <label class="janitor-field-label" for="janitor-uuid-output">Collected character IDs</label>
                <textarea id="janitor-uuid-output" readonly spellcheck="false"
                    placeholder="Character UUIDs found on this page will appear here."></textarea>
                <div id="janitor-uuid-actions">
                    <button type="button" id="janitor-uuid-scan" class="janitor-secondary">Watch for cards</button>
                    <button type="button" id="janitor-uuid-send" class="janitor-primary" disabled>Send to Datacat</button>
                    <button type="button" id="janitor-uuid-copy" class="janitor-secondary" disabled>Copy IDs</button>
                    <button type="button" id="janitor-uuid-copy-links" class="janitor-secondary" disabled>Copy links</button>
                    <button type="button" id="janitor-uuid-clear" class="janitor-quiet" disabled>Clear collection</button>
                </div>
            </div>
        `;

        const style = document.createElement("style");
        style.textContent = `
            #janitor-uuid-collector {
                position: fixed; right: 18px; bottom: 18px; z-index: 2147483647;
                width: min(420px, calc(100vw - 24px)); max-height: calc(100vh - 24px);
                padding: 14px; overflow: auto;
                border: 1px solid rgba(255,255,255,.12); border-radius: 16px;
                background: linear-gradient(160deg, rgba(30,30,39,.98), rgba(15,15,21,.98));
                color: #f7f7fb; box-shadow: 0 18px 60px rgba(0,0,0,.5), 0 1px 0 rgba(255,255,255,.05) inset;
                font: 14px/1.4 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
                backdrop-filter: blur(12px);
            }
            #janitor-uuid-collector * { box-sizing: border-box; }
            #janitor-uuid-header {
                display: flex; align-items: center; justify-content: space-between;
                gap: 10px; margin-bottom: 12px; cursor: move; user-select: none;
            }
            .janitor-title-group { display: flex; align-items: center; gap: 10px; min-width: 0; }
            .janitor-title-group > div { display: grid; min-width: 0; }
            .janitor-title-group strong { font-size: 14px; letter-spacing: .01em; }
            #janitor-uuid-header small { color: #9898a8; font-size: 10px; }
            .janitor-app-mark {
                display: grid; place-items: center; width: 30px; height: 30px;
                border-radius: 9px; background: linear-gradient(135deg,#8b78ff,#6251e8);
                box-shadow: 0 5px 16px rgba(112,92,255,.28); color: #fff; font-weight: 800;
            }
            #janitor-uuid-toggle {
                width: 30px; height: 30px; padding: 0; border: 0;
                border-radius: 8px; background: rgba(255,255,255,.08); color: #fff;
                cursor: pointer; font-size: 20px; line-height: 1;
            }
            #janitor-uuid-status {
                margin-bottom: 10px; padding: 8px 10px; border: 1px solid rgba(255,255,255,.07);
                border-radius: 9px; background: rgba(255,255,255,.045); color: #cfcfd8; font-size: 12px;
            }
            #janitor-uuid-status::before { content: ""; display: inline-block; width: 7px; height: 7px; margin-right: 7px; border-radius: 50%; background: #8b78ff; }
            #janitor-uuid-status[data-tone="success"]::before { background: #62d58a; }
            #janitor-uuid-status[data-tone="warning"]::before { background: #edb85d; }
            #janitor-uuid-status[data-tone="error"]::before { background: #ef7373; }
            .janitor-field-label { display: block; margin: 0 0 6px 2px; color: #aaaaba; font-size: 11px; font-weight: 650; }
            #janitor-uuid-output {
                display: block; width: 100%; height: 164px; resize: vertical;
                padding: 10px 11px; border: 1px solid #3c3c49; border-radius: 10px;
                outline: none; background: rgba(7,7,11,.72); color: #f5f5f5;
                font: 12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;
            }
            #janitor-uuid-actions {
                display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 7px; margin-top: 10px;
            }
            #janitor-uuid-actions button {
                min-width: 0; min-height: 36px; padding: 8px 9px; border: 1px solid transparent; border-radius: 9px;
                color: #fff; cursor: pointer; font: inherit; font-size: 12px; font-weight: 700;
            }
            #janitor-uuid-actions .janitor-primary { background: linear-gradient(135deg,#7c68ff,#6653ef); box-shadow: 0 5px 15px rgba(112,92,255,.2); }
            #janitor-uuid-actions .janitor-secondary { border-color: rgba(255,255,255,.08); background: rgba(255,255,255,.075); }
            #janitor-uuid-actions .janitor-quiet { grid-column: 1 / -1; min-height: 32px; background: transparent; color: #a7a7b5; }
            #janitor-uuid-actions button:not(:disabled):hover, #janitor-uuid-toggle:hover { filter: brightness(1.14); }
            #janitor-uuid-actions button:focus-visible, #janitor-uuid-toggle:focus-visible, #janitor-uuid-output:focus-visible { outline: 2px solid #9b8cff; outline-offset: 2px; }
            #janitor-uuid-actions button:disabled { cursor: not-allowed; opacity: .42; }
            #janitor-uuid-collector.minimised { width: 260px; }
            #janitor-uuid-collector.minimised #janitor-uuid-content { display: none; }
            #janitor-uuid-collector.minimised #janitor-uuid-header { margin-bottom: 0; }
            @media (max-width: 480px) {
                #janitor-uuid-collector {
                    right: 8px !important; left: 8px !important;
                    bottom: 8px !important; top: auto !important; width: auto;
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
            const prefix = scanIntervalId === null ? "Ready" : "Watching page";
            setStatus(
                status,
                `${prefix} · ${formatCount(count, "ID")} · +${lastScanAdded} last scan`,
                scanIntervalId === null ? "neutral" : "success"
            );
            copyButton.disabled = count === 0;
            copyLinksButton.disabled = count === 0;
            sendButton.disabled = count === 0;
            clearButton.disabled = count === 0;
        }

        function persistCollectorState(position = savedPosition) {
            savedPosition = position;
            localStorage.setItem(
                JANITOR_COLLECTOR_STORAGE_KEY,
                JSON.stringify({
                    ids: [...collectedIds],
                    position: savedPosition,
                    updatedAt: Date.now()
                })
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

        function clearCollector({ broadcast = true } = {}) {
            // Announce before publishing the empty set so peers clear their
            // in-memory copies instead of merging stale IDs back in.
            if (broadcast) {
                const clearedAt = Date.now();
                clearPublishedJanitorIds(clearedAt);
                GM_setValue(janitorClearKey, { id: TAB_ID, at: clearedAt });
            }
            if (scanIntervalId !== null) {
                clearInterval(scanIntervalId);
                scanIntervalId = null;
                scanButton.textContent = "Watch for cards";
            }
            scanGeneration++;
            collectedIds.clear();
            lastScanAdded = 0;
            persistCollectorState();
            refreshOutput();
        }

        GM_addValueChangeListener(janitorClearKey, (_key, _old, _value, remote) => {
            if (remote) clearCollector({ broadcast: false });
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

        async function copyIds() {
            await copyToClipboard([...collectedIds].join("\n"));
            setStatus(
                status,
                `Copied ${formatCount(collectedIds.size, "UUID")}.`,
                "success"
            );
        }

        async function copyJanitorLinks() {
            const links = [...collectedIds]
                .map((id) => `https://janitorai.com/characters/${id}`)
                .join("\n");
            await copyToClipboard(links);
            setStatus(
                status,
                `Copied ${formatCount(collectedIds.size, "Janitor link")}.`,
                "success"
            );
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
                setStatus(
                    status,
                    `Sent ${formatCount(ids.length, "ID")} to ${formatCount(openDatacatTabs, "active Datacat tab")}.`,
                    "success"
                );
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
                setStatus(status, "Could not open Datacat. Allow pop-ups, then try again.", "error");
                return;
            }
            target.focus?.();
            setStatus(
                status,
                `Opened Datacat with ${formatCount(ids.length, "ID")} ready to import.`,
                "success"
            );
        }

        function toggleScanning() {
            if (scanIntervalId !== null) {
                clearInterval(scanIntervalId);
                scanIntervalId = null;
                scanGeneration++;
                scanButton.textContent = "Watch for cards";
                refreshOutput();
                return;
            }

            const generation = ++scanGeneration;
            scanIntervalId = setInterval(() => {
                if (generation === scanGeneration) scanPage();
            }, 500);
            scanButton.textContent = "Stop watching";
            refreshOutput();
            scanPage();
        }

        scanButton.addEventListener("click", toggleScanning);
        copyButton.addEventListener("click", () => {
            copyIds().catch((error) => {
                setStatus(status, `Could not copy UUIDs: ${String(error)}`, "error");
            });
        });
        copyLinksButton.addEventListener("click", () => {
            copyJanitorLinks().catch((error) => {
                setStatus(status, `Could not copy Janitor links: ${String(error)}`, "error");
            });
        });
        sendButton.addEventListener("click", sendToDatacat);
        clearButton.addEventListener("click", () => {
            clearCollector();
        });
        toggleButton.addEventListener("click", () => {
            const minimised = !panel.classList.contains("minimised");
            setPanelMinimised(panel, toggleButton, minimised);
        });
    }

    const CONFIG = Object.freeze({
        storageKey: "datacat_bulk_retriever_v1",
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
        logs: [],
        pendingIds: [],
        currentIndex: 0,
        activeIndex: null,
        startedAtMs: null,
        workerTabId: null,
        statusMessage: "Ready.",
        statusTone: "neutral",
        progressCurrent: 0,
        progressTotal: 0
    };

    let latestQueueCapacity = null;
    let saveTimer = null;
    let existingCacheSaveTimer = null;
    let applyingSharedState = false;
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
            ? `Datacat queue: ${latestQueueCapacity.pendingCount ?? 0} / ${latestQueueCapacity.limit ?? "∞"}`
            : "Datacat queue: unavailable";
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

    function isLocalWorker() {
        return state.workerTabId === TAB_ID;
    }

    function persistJobProgress() {
        if (applyingSharedState || (state.running && !isLocalWorker())) return;
        if (!state.running) {
            const lease = getWorkerLease();
            if (
                lease?.tabId !== TAB_ID &&
                Number.isFinite(lease?.expiresAt) &&
                lease.expiresAt > Date.now()
            ) {
                return;
            }
            state.workerTabId = TAB_ID;
        }

        saveJob({
            workerTabId: state.workerTabId,
            running: state.running,
            paused: state.paused,
            stopping: state.stopping,
            pendingIds: state.pendingIds,
            currentIndex: state.currentIndex,
            results: state.results,
            logs: state.logs,
            statusMessage: state.statusMessage,
            statusTone: state.statusTone,
            progressCurrent: state.progressCurrent,
            progressTotal: state.progressTotal,
            inputValue: elements.input.value,
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
    panel.setAttribute("aria-labelledby", "datacat-panel-title");
    panel.innerHTML = `
        <header class="datacat-panel-header" title="Drag to move">
            <div class="datacat-title-group">
                <span class="datacat-app-mark" aria-hidden="true">D</span>
                <div>
                    <strong id="datacat-panel-title">Bulk retriever</strong>
                    <small id="datacat-version">Datacat · v${SCRIPT_VERSION}</small>
                </div>
            </div>
            <button type="button" id="datacat-toggle-panel" title="Minimise"
                aria-label="Minimise" aria-controls="datacat-panel-content" aria-expanded="true">−</button>
        </header>

        <div id="datacat-panel-content">
            <div class="datacat-field-heading">
                <label for="datacat-links-input">Character queue</label>
                <span id="datacat-count" data-tone="neutral">0 valid IDs</span>
            </div>
            <textarea
                id="datacat-links-input"
                spellcheck="false"
                aria-describedby="datacat-input-help datacat-count"
                placeholder="Paste JanitorAI links or character IDs. Spaces, commas and new lines are accepted."
            ></textarea>
            <div id="datacat-input-help" class="datacat-input-help">
                <span>Links, IDs, spaces and commas accepted</span>
                <kbd>Ctrl</kbd><span>+</span><kbd>Enter</kbd><span>to start</span>
            </div>

            <div class="datacat-settings-row">
                <label class="datacat-number-field">
                    <span>Delay <small>seconds</small></span>
                    <input id="datacat-delay-input" type="number" min="0" step="0.5">
                </label>

                <label class="datacat-number-field">
                    <span>Retries <small>per request</small></span>
                    <input id="datacat-retries-input" type="number" min="0" max="10" step="1">
                </label>

                <span id="datacat-queue-count" class="datacat-chip">Datacat queue: unavailable</span>
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
                <div class="datacat-progress-heading">
                    <span>Progress</span>
                    <span id="datacat-progress-label">0 / 0</span>
                </div>
                <progress id="datacat-progress" max="1" value="0" aria-label="Retrieval progress"></progress>
            </div>

            <div class="datacat-actions datacat-primary-actions">
                <button type="button" id="datacat-start-button">Start retrieval</button>
                <button type="button" id="datacat-pause-button" disabled>Pause</button>
                <button type="button" id="datacat-stop-button" disabled>Stop</button>
            </div>

            <div class="datacat-actions datacat-secondary-actions">
                <button type="button" id="datacat-retry-failed-button" disabled>Retry failed</button>
                <button type="button" id="datacat-copy-failed-button" disabled>Copy failed</button>
                <button type="button" id="datacat-export-button" disabled>Export JSON</button>
                <button type="button" id="datacat-clear-button">Clear all</button>
            </div>

            <div id="datacat-status" role="status" aria-live="polite" data-tone="neutral">Ready.</div>
            <div id="datacat-stats" aria-label="Retrieval results"></div>
            <details id="datacat-log-wrap">
                <summary>Activity log <span id="datacat-log-count">0</span></summary>
                <div id="datacat-log"></div>
            </details>
        </div>
    `;

    const style = document.createElement("style");
    style.textContent = `
        #datacat-bulk-panel {
            --dc-accent: #7c68ff;
            --dc-accent-strong: #6753ef;
            --dc-surface: rgba(255,255,255,.055);
            --dc-border: rgba(255,255,255,.1);
            position: fixed;
            z-index: 2147483647;
            width: min(480px, calc(100vw - 24px));
            max-height: calc(100vh - 24px);
            padding: 14px;
            overflow: auto;
            border: 1px solid rgba(255,255,255,.12);
            border-radius: 16px;
            background: linear-gradient(160deg,rgba(30,30,39,.98),rgba(15,15,21,.98));
            color: #f7f7fb;
            box-shadow: 0 18px 60px rgba(0,0,0,.5), 0 1px 0 rgba(255,255,255,.05) inset;
            font: 14px/1.4 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
            backdrop-filter: blur(12px);
            scrollbar-color: #50505e transparent;
        }
        #datacat-bulk-panel * { box-sizing: border-box; }
        .datacat-panel-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            margin-bottom: 14px;
            cursor: move;
            user-select: none;
        }
        .datacat-title-group {
            display: flex;
            align-items: center;
            gap: 10px;
            min-width: 0;
        }
        .datacat-title-group > div { display: grid; min-width: 0; }
        .datacat-title-group strong { font-size: 14px; letter-spacing: .01em; }
        #datacat-version { color: #9898a8; font-size: 10px; }
        .datacat-app-mark {
            display: grid; place-items: center; width: 30px; height: 30px;
            border-radius: 9px; background: linear-gradient(135deg,var(--dc-accent),var(--dc-accent-strong));
            box-shadow: 0 5px 16px rgba(112,92,255,.28); color: #fff; font-weight: 800;
        }
        #datacat-toggle-panel {
            width: 30px; height: 30px; padding: 0; border: 0;
            border-radius: 8px; background: rgba(255,255,255,.08); color: #fff;
            cursor: pointer; font-size: 20px; line-height: 1;
        }
        .datacat-field-heading, .datacat-progress-heading {
            display: flex; align-items: center; justify-content: space-between; gap: 10px;
        }
        .datacat-field-heading { margin: 0 2px 6px; }
        .datacat-field-heading label, .datacat-progress-heading > span:first-child {
            color: #d8d8e2; font-size: 12px; font-weight: 700;
        }
        #datacat-count { color: #a9a9b8; font-size: 11px; }
        #datacat-count[data-tone="success"] { color: #77d798; }
        #datacat-count[data-tone="warning"] { color: #edbd6c; }
        #datacat-links-input {
            display: block; width: 100%; min-height: 164px; max-height: 45vh;
            resize: vertical; padding: 11px 12px; border: 1px solid #3c3c49;
            border-radius: 10px; outline: none; background: rgba(7,7,11,.72);
            color: #f5f5f5;
            font: 12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;
            transition: border-color .15s, box-shadow .15s;
        }
        #datacat-links-input:focus { border-color: var(--dc-accent); box-shadow: 0 0 0 3px rgba(124,104,255,.13); }
        #datacat-links-input::placeholder { color: #747483; }
        .datacat-input-help {
            display: flex; align-items: center; gap: 4px; margin: 6px 2px 0;
            color: #858594; font-size: 10px;
        }
        .datacat-input-help > span:first-child { margin-right: auto; }
        .datacat-input-help kbd {
            padding: 1px 5px; border: 1px solid #444451; border-bottom-width: 2px;
            border-radius: 4px; background: #25252d; color: #bdbdc8; font: inherit;
        }
        .datacat-settings-row {
            display: grid; grid-template-columns: auto auto 1fr; align-items: end;
            gap: 8px; margin-top: 12px;
        }
        .datacat-number-field { display: grid; gap: 5px; color: #cfcfd8; font-size: 11px; }
        .datacat-number-field small { color: #777786; font-size: 9px; font-weight: 500; }
        .datacat-number-field input[type="number"] {
            width: 84px; height: 34px; padding: 5px 8px; border: 1px solid #3c3c49;
            border-radius: 8px; outline: none; background: rgba(7,7,11,.72); color: #fff;
            font: inherit;
        }
        .datacat-chip {
            justify-self: end; padding: 6px 8px; border: 1px solid var(--dc-border);
            border-radius: 999px; background: var(--dc-surface); color: #aaaab8; font-size: 10px;
        }
        .datacat-advanced {
            margin-top: 10px; padding: 9px 10px; border: 1px solid rgba(255,255,255,.06);
            border-radius: 10px; background: rgba(255,255,255,.04); color: #cfcfd8;
        }
        .datacat-advanced summary, #datacat-log-wrap summary { cursor: pointer; color: #bcbcca; font-size: 12px; font-weight: 700; }
        .datacat-advanced label { display: flex; align-items: flex-start; gap: 7px; margin-top: 8px; cursor: pointer; font-size: 12px; }
        .datacat-advanced input { accent-color: var(--dc-accent); }
        .datacat-progress-wrap {
            margin-top: 12px;
        }
        #datacat-progress {
            display: block; width: 100%; height: 7px; margin-top: 7px; border: 0;
            border-radius: 999px; overflow: hidden; background: #292932; appearance: none;
        }
        #datacat-progress::-webkit-progress-bar { background: #292932; }
        #datacat-progress::-webkit-progress-value { border-radius: 999px; background: linear-gradient(90deg,var(--dc-accent-strong),#9c8cff); }
        #datacat-progress::-moz-progress-bar { border-radius: 999px; background: linear-gradient(90deg,var(--dc-accent-strong),#9c8cff); }
        #datacat-progress-label {
            color: #a9a9b8; font-size: 11px; text-align: right;
        }
        .datacat-actions { display: grid; gap: 7px; margin-top: 10px; }
        .datacat-primary-actions { grid-template-columns: 2fr 1fr 1fr; }
        .datacat-secondary-actions { grid-template-columns: repeat(4,minmax(0,1fr)); }
        .datacat-actions button {
            min-width: 0; min-height: 36px; padding: 8px 7px; border: 1px solid transparent;
            border-radius: 9px; color: #fff; cursor: pointer; font: inherit; font-size: 12px; font-weight: 700;
        }
        #datacat-start-button { background: linear-gradient(135deg,var(--dc-accent),var(--dc-accent-strong)); box-shadow: 0 5px 15px rgba(112,92,255,.2); }
        #datacat-pause-button { border-color: rgba(237,184,93,.2); background: rgba(178,124,39,.22); color: #f0ca89; }
        #datacat-stop-button { border-color: rgba(239,115,115,.2); background: rgba(185,65,65,.2); color: #f09a9a; }
        .datacat-secondary-actions button { min-height: 32px; padding: 6px; border-color: rgba(255,255,255,.07); background: rgba(255,255,255,.055); color: #c8c8d2; font-size: 10px; }
        #datacat-clear-button { color: #e0a0a0; }
        .datacat-actions button:not(:disabled):hover, #datacat-toggle-panel:hover { filter: brightness(1.14); }
        #datacat-bulk-panel button:focus-visible, #datacat-bulk-panel input:focus-visible, #datacat-bulk-panel summary:focus-visible { outline: 2px solid #9b8cff; outline-offset: 2px; }
        .datacat-actions button:disabled { cursor: not-allowed; opacity: .42; }
        #datacat-status {
            margin-top: 10px; padding: 9px 10px; border: 1px solid rgba(255,255,255,.07);
            border-radius: 9px; background: rgba(255,255,255,.045); color: #dddde5;
            font-size: 12px; overflow-wrap: anywhere;
        }
        #datacat-status::before { content: ""; display: inline-block; width: 7px; height: 7px; margin-right: 7px; border-radius: 50%; background: var(--dc-accent); }
        #datacat-status[data-tone="success"]::before { background: #62d58a; }
        #datacat-status[data-tone="warning"]::before { background: #edb85d; }
        #datacat-status[data-tone="error"]::before { background: #ef7373; }
        #datacat-stats {
            display: flex; gap: 6px; min-height: 17px; margin-top: 7px; color: #aaaab5; font-size: 10px;
        }
        #datacat-stats span { flex: 1; padding: 5px 7px; border-radius: 7px; background: rgba(255,255,255,.035); text-align: center; }
        #datacat-stats .retrieved { color: #77d798; }
        #datacat-stats .failed { color: #ef9292; }
        #datacat-log-wrap { display: none; margin-top: 8px; }
        #datacat-log-count { display: inline-grid; place-items: center; min-width: 18px; height: 18px; margin-left: 4px; padding: 0 5px; border-radius: 999px; background: rgba(255,255,255,.08); font-size: 9px; }
        #datacat-log {
            max-height: 160px; margin-top: 7px; padding: 8px;
            overflow: auto; border: 1px solid rgba(255,255,255,.06); border-radius: 9px; background: rgba(7,7,11,.72);
            color: #bcbcc5;
            font: 11px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;
            white-space: pre-wrap; overflow-wrap: anywhere;
        }
        #datacat-log .success { color: #7fd49a; }
        #datacat-log .warning { color: #e2b96c; }
        #datacat-log .error { color: #eb8888; }
        #datacat-log .muted { color: #92929c; }
        #datacat-bulk-panel.minimised { width: 260px; overflow: hidden; }
        #datacat-bulk-panel.minimised #datacat-panel-content { display: none; }
        #datacat-bulk-panel.minimised .datacat-panel-header { margin-bottom: 0; }
        @media (max-width: 520px) {
            #datacat-bulk-panel {
                right: 8px !important; left: 8px !important;
                bottom: 8px !important; top: auto !important; width: auto;
            }
            .datacat-input-help > span:first-child { display: none; }
            .datacat-settings-row { grid-template-columns: auto auto 1fr; }
            .datacat-number-field input[type="number"] { width: 72px; }
            .datacat-secondary-actions { grid-template-columns: repeat(2,minmax(0,1fr)); }
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
        toggle: $("#datacat-toggle-panel", panel),
        status: $("#datacat-status", panel),
        stats: $("#datacat-stats", panel),
        log: $("#datacat-log", panel),
        logWrap: $("#datacat-log-wrap", panel),
        logCount: $("#datacat-log-count", panel),
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

    function statusTone(message) {
        if (
            /finished|copied|imported|queued|synced|ready to retrieve/i.test(
                message
            )
        ) {
            return "success";
        }
        if (/failed|error|could not|no valid|unexpected|aborted/i.test(message)) {
            return "error";
        }
        if (
            /waiting|paused|stopping|stopped|incomplete|busy|retry/i.test(
                message
            )
        ) {
            return "warning";
        }
        return "neutral";
    }

    function updateStatus(message, tone = statusTone(message)) {
        state.statusMessage = message;
        state.statusTone = tone;
        setStatus(elements.status, message, tone);
        console.log("[Datacat bulk]", message);
        persistJobProgress();
    }

    function renderLogs() {
        elements.log.textContent = "";
        for (const entry of state.logs) {
            const line = document.createElement("div");
            line.className = entry.type;
            line.textContent = `[${entry.time}] ${entry.message}`;
            elements.log.appendChild(line);
        }
        elements.logWrap.style.display = state.logs.length ? "block" : "none";
        elements.logCount.textContent = String(state.logs.length);
        elements.log.scrollTop = elements.log.scrollHeight;
    }

    function appendLog(message, type = "muted") {
        state.logs.push({ time: timestamp(), message, type });
        if (state.logs.length > CONFIG.maxLogLines) {
            state.logs.splice(0, state.logs.length - CONFIG.maxLogLines);
        }
        renderLogs();
        persistJobProgress();
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

        elements.export.disabled = !hasResults;
        elements.copyFailed.disabled = !hasFailures;
        elements.retryFailed.disabled = !hasFailures || state.running;
    }

    function updateStats() {
        const counts = countByStatus();
        elements.stats.innerHTML = `
            <span class="retrieved">${counts.retrieved} retrieved</span>
            <span>${counts.skipped} skipped</span>
            <span class="failed">${counts.failed} failed</span>
        `;
        updateResultActions();
    }

    function updateProgress(current, total) {
        state.progressCurrent = current;
        state.progressTotal = total;
        elements.progress.max = Math.max(1, total);
        elements.progress.value = Math.min(current, total);
        elements.progressLabel.textContent = `${current} / ${total}`;
        elements.progress.setAttribute(
            "aria-valuetext",
            `${current} of ${total}`
        );
        persistJobProgress();
    }

    function updateCount() {
        const { ids, invalid } = parseCharacterIds(elements.input.value);
        elements.count.textContent =
            formatCount(ids.length, "valid ID") +
            (invalid.length ? ` · ${formatCount(invalid.length, "invalid item")}` : "");
        elements.count.dataset.tone = invalid.length
            ? "warning"
            : ids.length
              ? "success"
              : "neutral";
        elements.input.setAttribute("aria-invalid", String(invalid.length > 0));
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

    function setIdleQueue(ids) {
        if (state.running) return;
        state.pendingIds = [...ids];
        state.currentIndex = 0;
        updateProgress(0, state.pendingIds.length);
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
        setIdleQueue(ids);
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
            if (!isLocalWorker()) return;
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
        setIdleQueue(ids);
        updateStatus(
            `Synced ${imported.length} ID${imported.length === 1 ? "" : "s"} from active Janitor tab${imported.length === 1 ? "" : "s"}.`
        );
    }

    function syncSharedJob(job) {
        if (state.running && isLocalWorker()) return;

        applyingSharedState = true;
        try {
            if (!job || !Array.isArray(job.pendingIds)) {
                state.running = false;
                state.paused = false;
                state.stopping = false;
                state.workerTabId = null;
                state.pendingIds = [];
                state.results = [];
                state.logs = [];
                state.currentIndex = 0;
                state.statusMessage = "Ready.";
                state.statusTone = "neutral";
                setInputValue("");
                renderLogs();
                updateProgress(0, 0);
                updateStats();
                setRunningUi(false);
                elements.pause.textContent = "Pause";
                setStatus(elements.status, "Ready.", "neutral");
                return;
            }

            const lease = getWorkerLease();
            const workerIsActive =
                job.running === true &&
                lease?.tabId === job.workerTabId &&
                lease.expiresAt > Date.now();

            state.workerTabId = job.workerTabId || null;
            state.running = workerIsActive;
            state.paused = workerIsActive && job.paused === true;
            state.stopping = workerIsActive && job.stopping === true;
            state.pendingIds = [...job.pendingIds];
            state.currentIndex = Number.isFinite(job.currentIndex)
                ? job.currentIndex
                : 0;
            state.results = Array.isArray(job.results) ? [...job.results] : [];
            state.logs = Array.isArray(job.logs) ? [...job.logs] : [];

            setInputValue(
                typeof job.inputValue === "string"
                    ? job.inputValue
                    : remainingQueueIds(
                          state.pendingIds,
                          state.results
                      ).join("\n")
            );
            applyJobOptions(job.options);
            renderLogs();
            updateProgress(
                Number.isFinite(job.progressCurrent)
                    ? job.progressCurrent
                    : state.results.length,
                Number.isFinite(job.progressTotal)
                    ? job.progressTotal
                    : state.pendingIds.length
            );
            updateStats();
            setRunningUi(state.running);
            elements.pause.textContent = state.paused ? "Resume" : "Pause";

            const message = workerIsActive
                ? job.statusMessage || "Retrieval is running."
                : job.running
                  ? "The worker tab closed. Start retrieval to continue the remaining queue."
                  : job.statusMessage || "Ready.";
            const tone = workerIsActive
                ? job.statusTone || "neutral"
                : job.running
                  ? "warning"
                  : job.statusTone || "neutral";
            state.statusMessage = message;
            state.statusTone = tone;
            setStatus(elements.status, message, tone);
        } finally {
            applyingSharedState = false;
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
        persistJobProgress();
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
        elements.pause.setAttribute("aria-pressed", String(state.paused));
        updateResultActions();
    }

    async function processIds(sourceIds) {
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

        state.workerTabId = TAB_ID;
        state.running = true;
        state.paused = false;
        state.stopping = false;
        state.results = [];
        state.pendingIds = [...sourceIds];
        // Live queue: pushing to state.pendingIds (e.g. Janitor sends mid-run)
        // extends this run because the loop and existence checks read it directly.
        const ids = state.pendingIds;
        state.currentIndex = 0;
        state.startedAtMs = Date.now();

        state.logs = [];
        elements.logWrap.open = false;
        renderLogs();
        persistJobProgress();

        elements.pause.textContent = "Pause";
        updateProgress(state.results.length, ids.length);
        updateStats();
        setRunningUi(true);

        appendLog(`Started queue with ${ids.length} character(s).`);

        const shouldCheckExisting =
            options.skipExisting && !options.alwaysReextract;
        const existingStatuses = new Map();
        const completedIds = new Set(state.results.map((result) => result.id));
        const startedIds = new Set();
        let nextExistenceCheckIndex = 0;
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
            let index = 0;
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
                const eta = formatEta(index, ids.length, state.startedAtMs);
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
        } catch (error) {
            if (error.message === "Stopped by user") {
                updateStatus(
                    `Stopped after ${state.results.length} of ${ids.length} characters.`
                );
                appendLog("Queue stopped by user.", "warning");
                persistJobProgress();
            } else if (isAuthError(error)) {
                updateStatus(
                    "Stopped: authentication failed. Refresh Datacat, then start the remaining queue."
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
            persistJobProgress();
        }
    }

    function stopImmediately() {
        if (!state.running || !isLocalWorker()) return;
        state.stopping = true;
        state.paused = false;
        elements.stop.disabled = true;
        elements.pause.disabled = true;
        updateStatus("Stopping…");
        for (const controller of state.requestControllers) controller.abort();
        state.sleepCancel?.();
    }

    function togglePause() {
        if (!state.running || !isLocalWorker()) return;
        state.paused = !state.paused;
        elements.pause.textContent = state.paused ? "Resume" : "Pause";
        elements.pause.setAttribute("aria-pressed", String(state.paused));
        updateStatus(state.paused ? "Paused." : "Resuming…");
        persistJobProgress();
    }

    function sendWorkerCommand(type) {
        if (!state.running || !state.workerTabId) return;
        GM_setValue(workerCommandKey, {
            id: `${TAB_ID}-${Date.now().toString(36)}`,
            type,
            targetTabId: state.workerTabId,
            sentByTabId: TAB_ID,
            at: Date.now()
        });
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

        await copyToClipboard(failed.join("\n"));
        updateStatus(`Copied ${formatCount(failed.length, "failed ID")}.`);
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
            setPanelMinimised(panel, elements.toggle, true);
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
            setIdleQueue(parseCharacterIds(elements.input.value).ids);
            GM_setValue(datacatInputKey, elements.input.value);
        }, CONFIG.saveDebounceMs);
    });
    GM_addValueChangeListener(datacatInputKey, (_key, _old, value, remote) => {
        if (!remote || (state.running && isLocalWorker())) return;
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
        if (isLocalWorker()) togglePause();
        else sendWorkerCommand("toggle-pause");
    });

    elements.stop.addEventListener("click", () => {
        if (isLocalWorker()) stopImmediately();
        else {
            sendWorkerCommand("stop");
            setStatus(elements.status, "Stopping…", "warning");
        }
    });

    elements.clear.addEventListener("click", () => {
        if (state.running) return;
        applyingSharedState = true;
        try {
            state.workerTabId = null;
            state.results = [];
            state.logs = [];
            state.pendingIds = [];
            state.currentIndex = 0;
            setInputValue("", { mirror: true });
            renderLogs();
            elements.logWrap.open = false;
            updateStatus("Ready.");
            updateProgress(0, 0);
            updateStats();
        } finally {
            applyingSharedState = false;
        }
        saveJob(null);
    });

    elements.retryFailed.addEventListener("click", () => {
        const failed = getFailedIds();
        if (!state.running && failed.length) {
            setInputValue(failed.join("\n"), { mirror: true });
            processIds(failed);
        }
    });

    elements.copyFailed.addEventListener("click", () => {
        copyFailedIds().catch((error) => {
            updateStatus(`Could not copy failed IDs: ${String(error)}`);
        });
    });

    elements.export.addEventListener("click", downloadResults);

    elements.toggle.addEventListener("click", () => {
        const minimised = !panel.classList.contains("minimised");
        setPanelMinimised(panel, elements.toggle, minimised);
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
            elements.stop.click();
        }
    });

    pageWindow.addEventListener("beforeunload", (event) => {
        if (existingCacheSaveTimer !== null) {
            clearTimeout(existingCacheSaveTimer);
            saveExistingIdCache();
        }
        if (!state.running || !isLocalWorker()) return;
        state.running = false;
        state.paused = false;
        state.statusMessage = "The worker tab closed.";
        state.statusTone = "warning";
        persistJobProgress();
        releaseWorkerLease();
        event.preventDefault();
        event.returnValue = "";
    });

    applySavedSettings();
    const initialSharedJob = loadJob();
    if (initialSharedJob) syncSharedJob(initialSharedJob);
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
    GM_addValueChangeListener(workerCommandKey, (_key, _old, command, remote) => {
        if (
            !remote ||
            !isLocalWorker() ||
            command?.targetTabId !== TAB_ID
        ) {
            return;
        }
        if (command.type === "toggle-pause") togglePause();
        else if (command.type === "stop") stopImmediately();
    });
    GM_addValueChangeListener(workerLeaseKey, (_key, _old, lease, remote) => {
        if (
            remote &&
            state.running &&
            isLocalWorker() &&
            lease?.tabId &&
            lease.tabId !== TAB_ID
        ) {
            updateStatus("Another tab took the retrieval worker; stopping this tab.");
            stopImmediately();
        }
    });
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
