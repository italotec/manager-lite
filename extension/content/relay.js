// ISOLATED-world relay: the only bridge between content/bridge.js (MAIN world, page session tokens)
// and the extension's chrome.* APIs (popup.js, background.js). MAIN and ISOLATED worlds share the DOM
// but not a JS global, so window.postMessage is the only channel to bridge.js; chrome.runtime messaging
// is the only channel to the rest of the extension.
(function () {
  const MSG_TO_BRIDGE = "ml-relay";
  const MSG_FROM_BRIDGE = "ml-bridge";

  let lastContext = { businessId: null, wabaId: null };
  const pending = new Map(); // reqId -> resolve fn

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.source !== MSG_FROM_BRIDGE) return;

    if (data.type === "context") {
      lastContext = data.context || lastContext;
    } else if (data.type === "queryResult") {
      const resolve = pending.get(data.reqId);
      if (resolve) {
        pending.delete(data.reqId);
        resolve(data.result);
      }
    }
  });

  function requestFreshContext(timeoutMs = 800) {
    return new Promise((resolve) => {
      window.postMessage({ source: MSG_TO_BRIDGE, type: "requestContext" }, "*");
      setTimeout(() => resolve(lastContext), timeoutMs);
    });
  }

  function runQuery(key, ctx, timeoutMs = 20000) {
    const reqId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        pending.delete(reqId);
        resolve({ key, ok: false, err: "timeout aguardando resposta da página" });
      }, timeoutMs);
      pending.set(reqId, (result) => {
        clearTimeout(timer);
        resolve(result);
      });
      window.postMessage({ source: MSG_TO_BRIDGE, type: "runQuery", reqId, key, ctx }, "*");
    });
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || typeof msg !== "object") return false;

    if (msg.type === "ml-get-context") {
      requestFreshContext().then((context) => sendResponse({ ok: true, context }));
      return true;
    }

    if (msg.type === "ml-run-query") {
      runQuery(msg.key, msg.ctx || {}).then((result) => sendResponse({ ok: true, result }));
      return true;
    }

    return false;
  });
})();
