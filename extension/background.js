// Service worker: holds the baked-in API key (config.js, replaced per-user by
// app/routes/extension_bp.py at download time), resolves the current AdsPower profile, and is the
// only part of the extension allowed to talk to Manager Lite's own API (host_permissions is scoped to
// baseUrl so this needs no CORS workaround).
//
// A broken config.js must never take the whole worker down silently (it did once — see the ML mark
// git history: config.js wrote to `window`, which doesn't exist in a service worker, so importScripts
// threw and chrome.runtime.onMessage never registered, and the popup hung on "Verificando…" forever).
// So this always finishes booting and reports the failure through the normal message responses
// instead of letting the SW die.
let CONFIG_ERROR = null;
try {
  importScripts("config.js");
} catch (e) {
  CONFIG_ERROR = String((e && e.message) || e);
}
const CFG = self.__ML_CONFIG__ || { apiKey: "", baseUrl: "http://localhost:5012" };
if (!self.__ML_CONFIG__ && !CONFIG_ERROR) {
  CONFIG_ERROR = "config.js não definiu __ML_CONFIG__.";
}

const PROFILE_CACHE_KEY = "mlProfileInfo";
const MANUAL_PROFILE_KEY = "mlManualProfileInfo";

function apiBase() {
  return (CFG.baseUrl || "").replace(/\/+$/, "");
}

function fetchWithTimeout(url, opts, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(url, Object.assign({}, opts, { signal: controller.signal }))
    .finally(() => clearTimeout(timer));
}

// ── AdsPower profile — manual override ──────────────────────────────────────
// chrome.storage.local is per Chrome profile, i.e. per AdsPower profile — typing this once here
// sticks for that profile forever, and always wins over auto-detection.

async function getManualProfile() {
  const stored = await chrome.storage.local.get(MANUAL_PROFILE_KEY);
  const m = stored[MANUAL_PROFILE_KEY];
  return m && m.profileId ? m : null;
}

async function setManualProfile(profileId, serialNumber) {
  const info = {
    profileId: String(profileId || "").trim(),
    serialNumber: String(serialNumber || "").trim(),
    manual: true,
    detectedAt: Date.now(),
  };
  await chrome.storage.local.set({ [MANUAL_PROFILE_KEY]: info });
  return info;
}

// ── AdsPower profile — auto-detection ────────────────────────────────────────
// https://start.adspower.net/?id= is rewritten by the AdsPower browser build itself, in-address-bar,
// to https://start.adspower.net/?id=<profile_id>&host=127.0.0.1:<local_api_port> — discovered by
// inspecting a live AdsPower session. http://<host>/api/getBrowserInfo?id=<id> then hands back the
// profile's serial number (accId/browser_head) and its remark, which — for profiles created by this
// project's own generator flow — carries the originating business_id as a JSON blob.

function isResolvedStartUrl(url) {
  return !!url && /[?&]id=/.test(url) && /[?&]host=/.test(url);
}

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

      const listener = (updatedTabId, changeInfo, updatedTab) => {
        if (updatedTabId !== tabId) return;
        if (isResolvedStartUrl(changeInfo.url)) finish(changeInfo.url);
        else if (changeInfo.status === "complete" && isResolvedStartUrl(updatedTab.url)) finish(updatedTab.url);
      };
      chrome.tabs.onUpdated.addListener(listener);

      const timer = setTimeout(() => finish(null), 4000);
    });
  });
}

async function resolveStartUrl() {
  // The address-bar rewrite only happens on a real top-level navigation done by the AdsPower browser
  // build itself — a service-worker fetch() never touches the address bar, so it can never observe it
  // (an earlier version tried this and it was dead code). Cheapest real signal: the profile's default
  // start tab is very often already sitting on start.adspower.net — check open tabs before resorting to
  // opening a new hidden one.
  try {
    const tabs = await chrome.tabs.query({ url: "https://start.adspower.net/*" });
    for (const t of tabs) {
      if (isResolvedStartUrl(t.url)) return t.url;
    }
  } catch (_) {}
  return resolveViaHiddenTab();
}

async function detectAdsPowerProfile(force) {
  const manual = await getManualProfile();
  if (manual) return manual;

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
    const r = await fetchWithTimeout(`http://${host}/api/getBrowserInfo?id=${encodeURIComponent(id)}`, {}, 5000);
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
  if (CONFIG_ERROR) {
    return { ok: false, error: `Extensão com configuração inválida: ${CONFIG_ERROR} Baixe novamente em Minha Conta > API.` };
  }
  if (!CFG.apiKey) {
    return { ok: false, error: "Extensão sem chave de API — baixe novamente em Minha Conta > API no Manager Lite." };
  }
  try {
    const resp = await fetchWithTimeout(`${apiBase()}/api/v1/me/extension-config`, {
      headers: { Authorization: `Bearer ${CFG.apiKey}` },
    }, 15000);
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) return { ok: false, error: body.error || `HTTP ${resp.status}` };
    return body;
  } catch (e) {
    return { ok: false, error: e && e.name === "AbortError" ? "timeout ao contatar o Manager Lite" : String(e) };
  }
}

async function registerWaba(payload) {
  if (CONFIG_ERROR) {
    return { ok: false, error: `Extensão com configuração inválida: ${CONFIG_ERROR} Baixe novamente em Minha Conta > API.` };
  }
  if (!CFG.apiKey) {
    return { ok: false, error: "Extensão sem chave de API — baixe novamente em Minha Conta > API no Manager Lite." };
  }
  try {
    const resp = await fetchWithTimeout(`${apiBase()}/api/v1/business-managers`, {
      method: "POST",
      headers: { Authorization: `Bearer ${CFG.apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }, 15000);
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok || body.ok === false) {
      return { ok: false, status: resp.status, error: body.error || `HTTP ${resp.status}` };
    }
    return { ok: true, status: resp.status, body };
  } catch (e) {
    return { ok: false, error: e && e.name === "AbortError" ? "timeout ao registrar no Manager Lite" : String(e) };
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
  if (msg.type === "ml-set-manual-profile") {
    setManualProfile(msg.profileId, msg.serialNumber)
      .then((info) => sendResponse({ ok: true, info }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  if (msg.type === "ml-clear-manual-profile") {
    chrome.storage.local.remove(MANUAL_PROFILE_KEY)
      .then(() => sendResponse({ ok: true }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  if (msg.type === "ml-register-waba") {
    registerWaba(msg.payload).then(sendResponse);
    return true;
  }
  return false;
});
