// Popup controller. Talks to content/relay.js (in the active tab) for anything that needs the page's
// own Facebook session, and to background.js for anything that needs Manager Lite's API or the
// AdsPower local API. Never touches either directly — same MAIN/ISOLATED/extension split as
// I:\Verificador Interface\waba_inspector.

const els = {
  noContext: document.getElementById("viewNoContext"),
  main: document.getElementById("viewMain"),
  businessId: document.getElementById("fBusinessId"),
  wabaId: document.getElementById("fWabaId"),
  profile: document.getElementById("fProfile"),
  tier: document.getElementById("fTier"),
  restrictionCard: document.getElementById("restrictionCard"),
  restrictionTitle: document.getElementById("restrictionTitle"),
  restrictionDetail: document.getElementById("restrictionDetail"),
  btnLink: document.getElementById("btnLink"),
  btnRefresh: document.getElementById("btnRefresh"),
  steps: document.getElementById("steps"),
  toast: document.getElementById("toast"),
  btnManualProfile: document.getElementById("btnManualProfile"),
  manualForm: document.getElementById("manualProfileForm"),
  manualProfileId: document.getElementById("manualProfileId"),
  manualSerialNumber: document.getElementById("manualSerialNumber"),
  btnManualSave: document.getElementById("btnManualSave"),
  btnManualCancel: document.getElementById("btnManualCancel"),
  btnManualRemove: document.getElementById("btnManualRemove"),
};

let state = {};

function resetState() {
  state = {
    tabId: state.tabId ?? null,
    businessId: null,
    wabaId: null,
    profile: null,
    config: null,
    restricted: null, // true | false | null (unknown)
    tierEnum: null,   // "TIER_10K" etc, same vocabulary as snapshot.messaging_limit_tier
    _resolvingBusiness: false,
  };
}
resetState();

// A tab we've already tried injecting into this popup session — never retry more than once, so a
// genuinely broken page can't loop forever.
const injectedTabs = new Set();

// Mirrors the dashboard's own label logic — app/templates/dashboard.html:544.
function tierLabel(tierEnum) {
  if (!tierEnum) return "—";
  return tierEnum === "TIER_UNLIMITED" ? "∞" : tierEnum.replace("TIER_", "");
}

// The exact response shape (data.WhatsAppBusinessMessagingLimitInfo.current_message_limit_tier) is one
// GraphQL alias away from breaking if Meta renames a field. Walking for the key itself survives that.
function findTierEnum(node) {
  if (!node || typeof node !== "object") return null;
  if (Array.isArray(node)) {
    for (const item of node) {
      const found = findTierEnum(item);
      if (found) return found;
    }
    return null;
  }
  if (typeof node.current_message_limit_tier === "string") return node.current_message_limit_tier;
  for (const v of Object.values(node)) {
    if (v && typeof v === "object") {
      const found = findTierEnum(v);
      if (found) return found;
    }
  }
  return null;
}

function withTimeout(promise, ms, timeoutError) {
  return new Promise((resolve) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) { settled = true; resolve({ ok: false, error: timeoutError }); }
    }, ms);
    promise.then((r) => {
      if (!settled) { settled = true; clearTimeout(timer); resolve(r); }
    });
  });
}

function _sendToTabRaw(tabId, msg) {
  return new Promise((resolve) => {
    chrome.tabs.sendMessage(tabId, msg, (resp) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message });
      } else {
        resolve(resp || { ok: false, error: "sem resposta da página" });
      }
    });
  });
}

function _sendToBgRaw(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (resp) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message });
      } else {
        resolve(resp || { ok: false, error: "sem resposta da extensão" });
      }
    });
  });
}

async function injectContentScripts(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId }, files: ["lib/queries.js", "content/bridge.js"], world: "MAIN",
    });
    await chrome.scripting.executeScript({
      target: { tabId }, files: ["content/relay.js"], world: "ISOLATED",
    });
    return true;
  } catch (e) {
    return false;
  }
}

// The manifest only auto-injects content scripts into navigations that happen *after* the extension
// loads — a tab that was already open on business.facebook.com when the extension was installed or
// reloaded never gets them, and every message here would otherwise hang until the 25s timeout with no
// way to recover short of a manual page reload. Detect that one specific failure and self-heal once.
async function sendToTab(tabId, msg, timeoutMs = 25000) {
  let resp = await withTimeout(_sendToTabRaw(tabId, msg), timeoutMs, "timeout aguardando resposta da página");
  const looksMissing = resp && resp.error &&
    /Receiving end does not exist|Could not establish connection/i.test(resp.error);
  if (looksMissing && !injectedTabs.has(tabId)) {
    injectedTabs.add(tabId);
    const injected = await injectContentScripts(tabId);
    if (injected) {
      await new Promise((r) => setTimeout(r, 300)); // let bridge.js's first pollContext() land
      resp = await withTimeout(_sendToTabRaw(tabId, msg), timeoutMs, "timeout aguardando resposta da página");
    }
  }
  return resp;
}

function sendToBg(msg, timeoutMs = 8000) {
  return withTimeout(_sendToBgRaw(msg), timeoutMs, "timeout aguardando resposta da extensão");
}

function showToast(msg, ok) {
  els.toast.textContent = msg;
  els.toast.className = "ml-toast " + (ok ? "ok" : "err");
  els.toast.classList.remove("hidden");
  setTimeout(() => els.toast.classList.add("hidden"), 5000);
}

function setRestrictionUI(status, title, detail) {
  els.restrictionCard.className = "ml-status-card ml-status-" + status;
  els.restrictionTitle.textContent = title;
  els.restrictionDetail.textContent = detail || "";
}

function refreshLinkButton() {
  const ready = !!(state.businessId && state.wabaId && state.config && state.config.ok !== false &&
    state.config.partner_business_id && state.restricted !== true);
  els.btnLink.disabled = !ready;
}

function renderProfileRow() {
  if (state.profile && state.profile.profileId) {
    const manualTag = state.profile.manual ? " (manual)" : "";
    els.profile.textContent =
      `${state.profile.profileId}${state.profile.serialNumber ? " · nº " + state.profile.serialNumber : ""}${manualTag}`;
  } else {
    els.profile.textContent = "não detectado";
  }
}

function extractWabasFromListResponse(data) {
  const out = [];
  const seen = new Set();
  function walk(node) {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) { node.forEach(walk); return; }
    const isWaba = node.assetType === "WHATSAPP_BUSINESS_ACCOUNT" ||
      node.business_asset_type === "WHATSAPP_BUSINESS_ACCOUNT";
    const id = node.assetID || node.business_object_id || node.id;
    if (isWaba && id && !seen.has(id)) {
      seen.add(id);
      out.push({ id: String(id), name: node.business_object_name || node.wabaName || "" });
    }
    Object.values(node).forEach(walk);
  }
  walk(data);
  return out;
}

async function resolveWabaId() {
  const listResp = await sendToTab(state.tabId, {
    type: "ml-run-query", key: "listWabas", ctx: { businessId: state.businessId },
  });
  const wabas = listResp.ok && listResp.result && listResp.result.ok
    ? extractWabasFromListResponse(listResp.result.data)
    : [];
  if (wabas.length) {
    state.wabaId = wabas[0].id;
    els.wabaId.textContent = state.wabaId;
  } else {
    els.wabaId.textContent = "—";
  }
}

async function checkRestriction() {
  if (!state.businessId) return;
  setRestrictionUI("checking", "Verificando…", "");
  const resp = await sendToTab(state.tabId, {
    type: "ml-run-query", key: "restriction", ctx: { businessId: state.businessId },
  });

  if (!resp.ok) {
    state.restricted = null;
    setRestrictionUI("bad", "Não foi possível acessar a página", "Recarregue a aba do Facebook e tente de novo.");
    refreshLinkButton();
    return;
  }

  const result = resp.result;
  const inner = result && result.ok && result.data && result.data.data;
  if (inner && "isRestricted" in inner) {
    state.restricted = !!inner.isRestricted;
    if (state.restricted) {
      setRestrictionUI("bad", "BM restrito", "Compartilhamento com parceiro é bloqueado pela Meta.");
    } else {
      setRestrictionUI("ok", "BM sem restrição", "");
    }
  } else {
    state.restricted = null;
    setRestrictionUI("warn", "Não foi possível confirmar", "Tente atualizar. O vínculo seguirá mesmo assim.");
  }
  refreshLinkButton();
}

async function loadTier() {
  if (!state.businessId || !state.wabaId) return;
  const resp = await sendToTab(state.tabId, {
    type: "ml-run-query", key: "limits", ctx: { businessId: state.businessId, wabaId: state.wabaId },
  });
  const data = resp.ok && resp.result && resp.result.ok ? resp.result.data : null;
  state.tierEnum = findTierEnum(data);
  els.tier.textContent = tierLabel(state.tierEnum);
}

// Restriction + tier only need the page's own session — never gated on Manager Lite or AdsPower being
// reachable, so a config/profile failure degrades the extension instead of freezing it.
async function resolveBusinessContext() {
  if (state._resolvingBusiness) return;
  state._resolvingBusiness = true;
  const restrictionPromise = checkRestriction();
  if (!state.wabaId) await resolveWabaId();
  const tierPromise = loadTier();
  await Promise.all([restrictionPromise, tierPromise]);
  state._resolvingBusiness = false;
  refreshLinkButton();
}

async function loadProfileAndConfig() {
  els.profile.textContent = "detectando…";
  const [profResp, cfgResp] = await Promise.all([
    sendToBg({ type: "ml-detect-profile" }),
    sendToBg({ type: "ml-get-config" }),
  ]);
  state.profile = profResp.info || null;
  state.config = cfgResp;
  renderProfileRow();

  if (!state.businessId && state.profile && state.profile.businessIdHint) {
    state.businessId = state.profile.businessIdHint;
    els.businessId.textContent = state.businessId;
    els.wabaId.textContent = state.wabaId || "verificando…";
    await resolveBusinessContext();
  }

  if (state.config && state.config.ok === false) {
    showToast(state.config.error || "Não foi possível carregar a configuração do Manager Lite.", false);
  }
  refreshLinkButton();
}

async function loadContext() {
  setRestrictionUI("checking", "Verificando…", "");

  // Fire-and-forget: profile/config never gate the restriction/tier check below.
  loadProfileAndConfig();

  const ctxResp = await sendToTab(state.tabId, { type: "ml-get-context" });
  if (!ctxResp.ok) {
    setRestrictionUI("bad", "Não foi possível acessar a página", "Recarregue a aba do Facebook e tente de novo.");
    els.wabaId.textContent = "—";
    refreshLinkButton();
    return;
  }

  const ctx = ctxResp.context || {};
  state.businessId = ctx.businessId || null;
  state.wabaId = ctx.wabaId || null;
  els.businessId.textContent = state.businessId || "—";
  els.wabaId.textContent = state.wabaId || (state.businessId ? "verificando…" : "—");

  if (state.businessId) {
    await resolveBusinessContext();
  } else {
    setRestrictionUI("idle", "Sem business_id", "Abra uma página de WABA com business_id na URL.");
    refreshLinkButton();
  }
}

function renderSteps(steps) {
  els.steps.classList.remove("hidden");
  els.steps.innerHTML = steps.map((s) => {
    const icon = s.status === "done" ? "&#10003;" : s.status === "error" ? "&#10007;"
      : s.status === "active" ? "&#8635;" : "&#9675;";
    const cls = s.status === "done" ? "ml-step-done" : s.status === "error" ? "ml-step-error"
      : s.status === "active" ? "ml-step-active" : "";
    return `<div class="ml-step ${cls}"><span class="ml-step-icon">${icon}</span><span>${s.label}</span></div>`;
  }).join("");
}

async function runLinkFlow() {
  els.btnLink.disabled = true;
  const steps = [
    { key: "restriction", label: "Verificando restrição", status: "active" },
    { key: "share", label: "Adicionando parceiro no BM", status: "pending" },
    { key: "register", label: "Registrando no Manager Lite", status: "pending" },
  ];
  renderSteps(steps);

  // 1) Restriction — re-checked right before acting, not just trusted from page load.
  const restrictionResp = await sendToTab(state.tabId, {
    type: "ml-run-query", key: "restriction", ctx: { businessId: state.businessId },
  });
  const rInner = restrictionResp.ok && restrictionResp.result && restrictionResp.result.ok &&
    restrictionResp.result.data && restrictionResp.result.data.data;
  if (rInner && rInner.isRestricted === true) {
    steps[0].status = "error";
    renderSteps(steps);
    setRestrictionUI("bad", "BM restrito", "Compartilhamento com parceiro é bloqueado pela Meta.");
    showToast("BM restrito — não é possível adicionar o parceiro.", false);
    els.btnLink.disabled = false;
    return;
  }
  steps[0].status = "done";
  steps[1].status = "active";
  renderSteps(steps);

  // 2) Share the WABA to the partner BM.
  const shareResp = await sendToTab(state.tabId, {
    type: "ml-run-query", key: "sharePartner",
    ctx: { businessId: state.businessId, wabaId: state.wabaId, partnerBusinessId: state.config.partner_business_id },
  });
  const shareConn = shareResp.ok && shareResp.result && shareResp.result.ok && shareResp.result.data &&
    shareResp.result.data.business_settings_add_partner_to_assets_connection;
  const shareOk = !!(shareConn && shareConn.results && shareConn.results.some((r) =>
    r.results && r.results.some((rr) => rr.result_type === "SUCCESS")));
  if (!shareOk) {
    steps[1].status = "error";
    renderSteps(steps);
    showToast("Falha ao adicionar o parceiro no Business Manager.", false);
    els.btnLink.disabled = false;
    return;
  }
  steps[1].status = "done";
  steps[2].status = "active";
  renderSteps(steps);

  // 3) Register the WABA in Manager Lite with the full data set.
  const payload = {
    waba_id: state.wabaId,
    token: state.config.meta_token,
    adspower_profile_id: state.profile ? state.profile.profileId : "",
    serial_number: state.profile ? state.profile.serialNumber : "",
    business_manager_id: state.businessId,
    messaging_limit_tier: state.tierEnum || undefined,
  };
  const regResp = await sendToBg({ type: "ml-register-waba", payload });
  if (!regResp.ok) {
    steps[2].status = "error";
    renderSteps(steps);
    showToast(regResp.error || "Falha ao registrar no Manager Lite.", false);
    els.btnLink.disabled = false;
    return;
  }
  steps[2].status = "done";
  renderSteps(steps);
  showToast("WABA vinculada ao Manager Lite com sucesso.", true);
  refreshLinkButton();
}

// ── Manual AdsPower profile override ─────────────────────────────────────────

els.btnManualProfile.addEventListener("click", () => {
  if (state.profile) {
    els.manualProfileId.value = state.profile.profileId || "";
    els.manualSerialNumber.value = state.profile.serialNumber || "";
  }
  els.manualForm.classList.toggle("hidden");
});

els.btnManualCancel.addEventListener("click", () => els.manualForm.classList.add("hidden"));

els.btnManualSave.addEventListener("click", async () => {
  const profileId = els.manualProfileId.value.trim();
  if (!profileId) { showToast("Informe o ID do perfil.", false); return; }
  const resp = await sendToBg({ type: "ml-set-manual-profile", profileId, serialNumber: els.manualSerialNumber.value.trim() });
  if (resp.ok) {
    state.profile = resp.info;
    renderProfileRow();
    els.manualForm.classList.add("hidden");
    refreshLinkButton();
    showToast("Perfil salvo para este navegador.", true);
  } else {
    showToast(resp.error || "Falha ao salvar.", false);
  }
});

els.btnManualRemove.addEventListener("click", async () => {
  await sendToBg({ type: "ml-clear-manual-profile" });
  els.manualForm.classList.add("hidden");
  await loadProfileAndConfig();
  showToast("Perfil manual removido.", true);
});

// ── Boot ──────────────────────────────────────────────────────────────────────

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || !/^https:\/\/business\.facebook\.com\//.test(tab.url)) {
    els.noContext.classList.remove("hidden");
    els.main.classList.add("hidden");
    return;
  }
  resetState();
  state.tabId = tab.id;
  els.noContext.classList.add("hidden");
  els.main.classList.remove("hidden");
  els.businessId.textContent = "—";
  els.wabaId.textContent = "—";
  els.tier.textContent = "—";
  els.steps.classList.add("hidden");
  await loadContext();
}

els.btnRefresh.addEventListener("click", init);
els.btnLink.addEventListener("click", runLinkFlow);

init();
