// Service worker: holds the baked-in API key (config.js, replaced per-user by
// app/routes/extension_bp.py at download time), resolves the current AdsPower profile, and is the
// only part of the extension allowed to talk to Manager Lite's own API (host_permissions is scoped to
// baseUrl so this needs no CORS workaround).
importScripts("config.js");

const CFG = self.__ML_CONFIG__ || { apiKey: "", baseUrl: "http://localhost:5012" };
const PROFILE_CACHE_KEY = "mlProfileInfo";

function apiBase() {
  return (CFG.baseUrl || "").replace(/\/+$/, "");
}

// ── AdsPower profile auto-detection ─────────────────────────────────────────
// https://start.adspower.net/?id= is rewritten by the AdsPower browser build itself, in-address-bar,
// to https://start.adspower.net/?id=<profile_id>&host=127.0.0.1:<local_api_port> — discovered by
// inspecting a live AdsPower session. http://<host>/api/getBrowserInfo?id=<id> then hands back the
// profile's serial number (accId/browser_head) and its remark, which — for profiles created by this
// project's own generator flow — carries the originating business_id as a JSON blob.

function resolveViaHiddenTab() {
  return new Promise((resolve) => {
    chrome.tabs.create({ url: "https://start.adspower.net/?id=", active: false }, (tab) => {
      if (!tab || !tab.id) { resolve(null); return; }
      const tabId = tab.id;
      let settled = false;

      const finish = (result) => {
        if (settled) return;
        settled = true;
        chrome.tabs.onUpdated.removeListener(listener);
        clearTimeout(timer);
        chrome.tabs.remove(tabId).catch(() => {});
        resolve(result);
      };

      const isResolved = (url) => !!url && /[?&]id=/.test(url) && /[?&]host=/.test(url);

      const listener = (updatedTabId, changeInfo, updatedTab) => {
        if (updatedTabId !== tabId) return;
        if (isResolved(changeInfo.url)) finish(changeInfo.url);
        else if (changeInfo.status === "complete" && isResolved(updatedTab.url)) finish(updatedTab.url);
      };
      chrome.tabs.onUpdated.addListener(listener);

      const timer = setTimeout(() => finish(null), 4000);
    });
  });
}

async function resolveStartUrl() {
  // Try a plain fetch first — cheap and invisible to the user, works if the rewrite happens at the
  // network layer (an HTTP redirect this profile's local proxy answers with). Falls back to an actual
  // navigation, since a redirect-only rewrite may not fire for a fetch() that never touches the address
  // bar.
  try {
    const resp = await fetch("https://start.adspower.net/?id=", { redirect: "follow" });
    if (resp && resp.url && /[?&]id=/.test(resp.url) && /[?&]host=/.test(resp.url)) {
      return resp.url;
    }
  } catch (_) {}
  return resolveViaHiddenTab();
}

async function detectAdsPowerProfile(force) {
  if (!force) {
    const cached = await chrome.storage.local.get(PROFILE_CACHE_KEY);
    if (cached[PROFILE_CACHE_KEY] && cached[PROFILE_CACHE_KEY].profileId) {
      return cached[PROFILE_CACHE_KEY];
    }
  }

  const resolvedUrl = await resolveStartUrl();
  if (!resolvedUrl) return null;

  const url = new URL(resolvedUrl);
  const id = url.searchParams.get("id");
  const host = url.searchParams.get("host");
  if (!id || !host) return null;

  const info = { profileId: id, serialNumber: "", businessIdHint: "", groupName: "", detectedAt: Date.now() };
  try {
    const r = await fetch(`http://${host}/api/getBrowserInfo?id=${encodeURIComponent(id)}`);
    const body = await r.json();
    if (body && body.code === 0 && body.data) {
      info.serialNumber = String(body.data.accId || body.data.browser_head || "");
      info.groupName = body.data.batch_name || "";
      const comment = body.data.comment || "";
      const m = comment.match(/"business_id"\s*:\s*"(\d+)"/);
      if (m) info.businessIdHint = m[1];
    }
  } catch (_) {
    // getBrowserInfo failing still leaves us the profile id — better than nothing.
  }

  await chrome.storage.local.set({ [PROFILE_CACHE_KEY]: info });
  return info;
}

// ── Manager Lite API ─────────────────────────────────────────────────────────

async function fetchExtensionConfig() {
  if (!CFG.apiKey) {
    return { ok: false, error: "Extensão sem chave de API — baixe novamente em Minha Conta > API no Manager Lite." };
  }
  try {
    const resp = await fetch(`${apiBase()}/api/v1/me/extension-config`, {
      headers: { Authorization: `Bearer ${CFG.apiKey}` },
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) return { ok: false, error: body.error || `HTTP ${resp.status}` };
    return body;
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

async function registerWaba(payload) {
  if (!CFG.apiKey) {
    return { ok: false, error: "Extensão sem chave de API — baixe novamente em Minha Conta > API no Manager Lite." };
  }
  try {
    const resp = await fetch(`${apiBase()}/api/v1/business-managers`, {
      method: "POST",
      headers: { Authorization: `Bearer ${CFG.apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok || body.ok === false) {
      return { ok: false, status: resp.status, error: body.error || `HTTP ${resp.status}` };
    }
    return { ok: true, status: resp.status, body };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

// ── Message router (popup.js -> here) ────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return false;

  if (msg.type === "ml-get-config") {
    fetchExtensionConfig().then(sendResponse);
    return true;
  }
  if (msg.type === "ml-detect-profile") {
    detectAdsPowerProfile(!!msg.force)
      .then((info) => sendResponse({ ok: !!info, info }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  if (msg.type === "ml-register-waba") {
    registerWaba(msg.payload).then(sendResponse);
    return true;
  }
  return false;
});
