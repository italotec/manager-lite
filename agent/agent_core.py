"""
agent_core.py — WebSocket agent core for the Manager Lite card client.

A single persistent connection to Manager Lite's /agent/ws endpoint. On
{"type":"add_card"} it opens the AdsPower profile, attaches Playwright over
CDP, runs the verified card-add automation (services.facebook_card), and
replies with a card_result frame. Also sends a periodic browser_status ping.

Call init(adspower_client) once before connect_loop().
"""
import asyncio
import json
import threading

import websockets
import websockets.exceptions

# ── Module-level init ─────────────────────────────────────────────────────────

_client = None  # AdsPowerClient — set by init()
_open_pids: set[str] = set()
_inactive_count: dict[str, int] = {}
_INACTIVE_THRESHOLD = 3


def init(adspower_client):
    """Call once from agent_gui.py before connecting."""
    global _client
    _client = adspower_client


# ── Card-add handler ───────────────────────────────────────────────────────────

def _execute_add_card_sync(msg: dict, log=print) -> dict:
    """Open the AdsPower profile, attach Playwright over CDP, run the card-add
    automation. Returns a card_result frame."""
    cmd_id      = msg.get("cmd_id")
    profile_id  = msg.get("profile_id", "")
    card        = msg.get("card", {}) or {}
    business_id = msg.get("business_id", "") or ""
    waba_id     = msg.get("waba_id", "") or ""

    def _result(ok: bool, **extra) -> dict:
        return {"type": "card_result", "cmd_id": cmd_id, "ok": ok, **extra}

    if not profile_id:
        return _result(False, error="profile_id ausente no comando")

    log(f"[CARD {profile_id}] Iniciando add_card (•••• {str(card.get('number',''))[-4:]})")

    try:
        from services.adspower import connect_cdp_with_retry
        from playwright.sync_api import sync_playwright
        from services import facebook_card
        from services import facebook_link
    except Exception as exc:
        return _result(False, error=f"Falha ao importar dependências: {exc}")

    try:
        browser_data = _client.open_browser(profile_id)
        _open_pids.add(profile_id)
    except Exception as exc:
        return _result(False, error=f"Falha ao abrir perfil AdsPower: {exc}")

    ws_endpoint = (browser_data.get("ws") or {}).get("puppeteer", "")
    if not ws_endpoint:
        try:
            _client.close_browser(profile_id)
        except Exception:
            pass
        return _result(False, error="Sem WebSocket endpoint do AdsPower")

    try:
        with sync_playwright() as p:
            browser, _ws = connect_cdp_with_retry(
                p, ws_endpoint,
                profile_id=profile_id,
                ads_client=_client,
            )
            ctx  = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()

            if not business_id:
                business_id = facebook_link.resolve_owning_business_id(page, log=log) or ""
                if not business_id:
                    return _result(False, error="Não foi possível resolver o business_id do perfil")

            res = facebook_card.add_card_via_cdp(page, card, business_id=business_id, waba_id=waba_id, log=log)
            return _result(
                bool(res.get("ok")),
                code=res.get("code"),
                credential_id=res.get("credential_id"),
                stage=res.get("stage"),
                error=res.get("error", "") or "",
            )

    except Exception as exc:
        import traceback as _tb
        log(f"[CARD {profile_id}] Exceção no browser: {exc}")
        print(_tb.format_exc(), flush=True)
        return _result(False, error=str(exc)[:500])
    finally:
        try:
            _client.close_browser(profile_id)
        except Exception:
            pass
        _open_pids.discard(profile_id)


async def _handle_add_card(msg: dict, outbox: asyncio.Queue, log=print):
    result = await asyncio.to_thread(_execute_add_card_sync, msg, log)
    await outbox.put(json.dumps(result))


# ── Vincular ao Manager (link_waba) handler ────────────────────────────────────

_LINK_SEMAPHORE = asyncio.Semaphore(5)


def _wait_for_fb_token(page, timeout: int = 120000) -> None:
    """Wait until Facebook's DTSG token is available in the page."""
    try:
        page.wait_for_function(
            """() => {
                try { if (require('DTSGInitialData').token) return true; } catch(e) {}
                if (document.querySelector('input[name="fb_dtsg"]')) return true;
                return /"DTSGInitialData",\\[\\],\\{"token":"[^"]+/.test(
                    document.documentElement.innerHTML);
            }""",
            timeout=timeout,
        )
    except Exception:
        pass  # the GraphQL helpers derive their own tokens and report missing ones


def _execute_link_waba_sync(msg: dict, log=print) -> dict:
    """Open the AdsPower profile, share the WABA to the partner BM, register
    it with Manager Lite. Returns a link_done frame."""
    profile_id       = msg["profile_id"]
    business_id      = msg.get("business_id") or ""
    waba_id          = msg.get("waba_id") or ""
    waba_name        = msg.get("waba_name") or ""
    partner_biz_id   = msg["partner_business_id"]
    meta_token       = msg["meta_token"]
    manager_api_key  = msg["manager_api_key"]
    manager_base_url = msg.get("manager_base_url") or ""

    def _result(status: str, message: str = "", **extra) -> dict:
        return {
            "type": "link_done",
            "profile_id": profile_id,
            "status": status,
            "message": message,
            "waba_id": waba_id,
            "business_id": business_id,
            **extra,
        }

    log(f"[LINK {profile_id}] Iniciando vincular")

    try:
        from services.adspower import connect_cdp_with_retry
        from services import facebook_link
        from services.manager_api import register_business_manager
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return _result("error", f"Falha ao importar dependências: {exc}")

    try:
        browser_data = _client.open_browser(profile_id)
        _open_pids.add(profile_id)
    except Exception as exc:
        return _result("error", f"Falha ao abrir perfil AdsPower: {exc}")

    ws_endpoint = (browser_data.get("ws") or {}).get("puppeteer", "")
    if not ws_endpoint:
        try:
            _client.close_browser(profile_id)
        except Exception:
            pass
        return _result("error", "Sem WebSocket endpoint do AdsPower")

    try:
        with sync_playwright() as p:
            browser, _ws = connect_cdp_with_retry(
                p, ws_endpoint,
                profile_id=profile_id,
                ads_client=_client,
            )
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()

            if not business_id:
                business_id = facebook_link.resolve_owning_business_id(page, log=log) or ""
                if not business_id:
                    return _result("error", "Não foi possível resolver o business_id do perfil")

            page.goto(
                f"https://business.facebook.com/latest/settings/whatsapp_account?business_id={business_id}",
                timeout=60000, wait_until="domcontentloaded",
            )
            _wait_for_fb_token(page)

            if not waba_id:
                waba_id = facebook_link.extract_waba_id_graphql(
                    page, business_id, expected_name=waba_name or None, log=log
                )
                if not waba_id:
                    return _result("error", "Não foi possível identificar o ID da WABA")

            try:
                ok = facebook_link.share_waba_graphql(page, business_id, partner_biz_id, waba_id, log=log)
            except facebook_link.BmRestrictedException as exc:
                return _result("restrita", str(exc))

            if not ok:
                return _result("error", f"Falha ao compartilhar WABA {waba_id} com o BM parceiro")

            serial_number = ""
            try:
                serial_number = str((_client.get_profile(profile_id) or {}).get("serial_number") or "")
            except Exception:
                pass

            reg = register_business_manager(
                base_url=manager_base_url,
                api_key=manager_api_key,
                waba_id=waba_id,
                token=meta_token,
                adspower_profile_id=profile_id,
                serial_number=serial_number,
            )
            if not reg["ok"]:
                return _result("error", f"Falha ao registrar no Manager Lite: {reg.get('error')}")

            log(f"[LINK {profile_id}] Concluído — waba_id={waba_id}")
            return {
                "type": "link_done", "profile_id": profile_id,
                "status": "ok", "message": "",
                "waba_id": waba_id, "business_id": business_id,
                "shared": True, "registered": True,
            }

    except Exception as exc:
        import traceback as _tb
        log(f"[LINK {profile_id}] Exceção no browser: {exc}")
        print(_tb.format_exc(), flush=True)
        return _result("error", str(exc)[:500])
    finally:
        try:
            _client.close_browser(profile_id)
        except Exception:
            pass
        _open_pids.discard(profile_id)


async def _handle_link_waba(msg: dict, outbox: asyncio.Queue, log=print):
    profile_id = msg.get("profile_id")
    await outbox.put(json.dumps({"type": "link_start", "profile_id": profile_id}))
    async with _LINK_SEMAPHORE:
        result = await asyncio.to_thread(_execute_link_waba_sync, msg, log)
    await outbox.put(json.dumps(result))


# ── Verificar-group profile sync ───────────────────────────────────────────────

async def _sync_profiles(outbox: asyncio.Queue, log=print):
    try:
        import config as _cfg

        def _collect():
            group_data = _client._get("/api/v1/group/list", page=1, page_size=200)
            name_to_id = {
                g["group_name"]: str(g["group_id"])
                for g in group_data.get("list", [])
            }
            gid = name_to_id.get(_cfg.VERIFICAR_GROUP_NAME)
            if not gid:
                return []
            profiles = []
            for p in _client.list_profiles(group_id=gid):
                profiles.append({
                    "profile_id": p["user_id"],
                    "name":       p.get("name", ""),
                    "group_name": _cfg.VERIFICAR_GROUP_NAME,
                    "remark":     p.get("remark", ""),
                })
            return profiles

        profiles = await asyncio.to_thread(_collect)
        await outbox.put(json.dumps({"type": "profiles_push", "profiles": profiles}))
        log(f"[SYNC] {len(profiles)} perfis do grupo Verificar enviados")
    except Exception as e:
        log(f"[SYNC] Falha ao sincronizar perfis: {e}")


async def _profile_sync_pinger(outbox: asyncio.Queue, stop: asyncio.Event, log=print, interval: float = 30.0):
    await _sync_profiles(outbox, log)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break
        await _sync_profiles(outbox, log)


# ── List-all-profiles handler ──────────────────────────────────────────────────

def _execute_list_profiles_sync(msg: dict, log=print) -> dict:
    cmd_id = msg.get("cmd_id")
    try:
        group_data = _client._get("/api/v1/group/list", page=1, page_size=200)
        id_to_name = {str(g["group_id"]): g["group_name"] for g in group_data.get("list", [])}
        raw = _client.list_profiles(group_id="")
        profiles = [
            {
                "profile_id": p["user_id"],
                "name": p.get("name", ""),
                "group_name": id_to_name.get(str(p.get("group_id", "")), ""),
            }
            for p in raw
        ]
        return {"type": "list_profiles_result", "cmd_id": cmd_id, "ok": True, "profiles": profiles}
    except Exception as exc:
        return {"type": "list_profiles_result", "cmd_id": cmd_id, "ok": False, "error": str(exc)[:500]}


async def _handle_list_profiles(msg: dict, outbox: asyncio.Queue, log=print):
    result = await asyncio.to_thread(_execute_list_profiles_sync, msg, log)
    await outbox.put(json.dumps(result))


# ── WABA health scan handler ────────────────────────────────────────────────────

_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_TIMED_OUT", "ERR_CONNECTION_TIMED_OUT", "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_SOCKS_CONNECTION_FAILED",
)


def _execute_scan_profile_sync(msg: dict, log=print) -> dict:
    """Open the AdsPower profile, scan every WABA it can reach for health
    (approved/appealable/in_review/permanent/restricted), optionally appeal
    appealable ones, and report profile-level checkpoint/not_logged_in/error."""
    cmd_id      = msg.get("cmd_id")
    profile_id  = msg.get("profile_id", "")
    auto_appeal = bool(msg.get("auto_appeal"))

    def _result(state: str, wabas=None, detail: str = "") -> dict:
        return {
            "type": "scan_result", "cmd_id": cmd_id, "ok": True,
            "profile_id": profile_id, "state": state,
            "wabas": wabas or [], "detail": detail,
        }

    if not profile_id:
        return {"type": "scan_result", "cmd_id": cmd_id, "ok": False, "error": "profile_id ausente no comando"}

    log(f"[SCAN {profile_id}] Iniciando scan")

    try:
        from services.adspower import connect_cdp_with_retry
        from playwright.sync_api import sync_playwright
        from services import facebook_scan
    except Exception as exc:
        return {"type": "scan_result", "cmd_id": cmd_id, "ok": False, "error": f"Falha ao importar dependências: {exc}"}

    try:
        browser_data = _client.open_browser(profile_id)
        _open_pids.add(profile_id)
    except Exception as exc:
        return {"type": "scan_result", "cmd_id": cmd_id, "ok": False, "error": f"Falha ao abrir perfil AdsPower: {exc}"}

    ws_endpoint = (browser_data.get("ws") or {}).get("puppeteer", "")
    if not ws_endpoint:
        try:
            _client.close_browser(profile_id)
        except Exception:
            pass
        return {"type": "scan_result", "cmd_id": cmd_id, "ok": False, "error": "Sem WebSocket endpoint do AdsPower"}

    try:
        with sync_playwright() as p:
            browser, _ws = connect_cdp_with_retry(p, ws_endpoint, profile_id=profile_id, ads_client=_client)

            def _fresh_page():
                # Open the new page BEFORE closing the old ones — closing every
                # tab in a context first can close the window itself (last-tab
                # behavior), which then makes new_page() fail with
                # "Target.createTarget: Failed to open a new tab".
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                old_pages = list(ctx.pages)
                new_page = ctx.new_page()
                for pg in old_pages:
                    try:
                        pg.close()
                    except Exception:
                        pass
                return new_page

            page = _fresh_page()
            target_url = "https://business.facebook.com/latest/settings/whatsapp_account"

            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                if not any(marker in str(exc) for marker in _PROXY_ERROR_MARKERS):
                    raise
                log(f"[SCAN {profile_id}] Erro de proxy detectado — removendo proxy e recarregando perfil")
                try:
                    _client.update_profile(profile_id, user_proxy_config={"proxy_soft": "no_proxy"})
                except Exception as exc2:
                    log(f"[SCAN {profile_id}] Falha ao remover proxy: {exc2}")
                try:
                    _client.close_browser(profile_id)
                except Exception:
                    pass
                import time as _time
                _time.sleep(2.0)
                fresh = _client.open_browser(profile_id)
                new_ws = (fresh.get("ws") or {}).get("puppeteer", "")
                browser, _ws = connect_cdp_with_retry(p, new_ws, profile_id=profile_id, ads_client=_client)
                page = _fresh_page()
                page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)

            page.wait_for_timeout(1500)

            if facebook_scan.is_checkpoint_url(page.url):
                log(f"[SCAN {profile_id}] Checkpoint detectado")
                return _result("checkpoint")

            if facebook_scan.is_login_url(page.url) or not facebook_scan.is_logged_in(page, log=log):
                log(f"[SCAN {profile_id}] Perfil não logado")
                return _result("not_logged_in")

            uid = facebook_scan.get_uid(page, log=log)
            business_ids = facebook_scan.enumerate_business_ids(page, log=log)

            if facebook_scan.is_checkpoint_url(page.url):
                log(f"[SCAN {profile_id}] Checkpoint detectado ao enumerar BMs")
                return _result("checkpoint")

            wabas_out = []
            for bm in business_ids:
                business_id = bm["id"]
                for w in facebook_scan.list_all_waba_assets(page, business_id, log=log):
                    waba_id = w["id"]
                    assessment = facebook_scan.assess_waba(page, business_id, waba_id, log=log)
                    appeal_sent = False
                    if auto_appeal and assessment["state"] == "appealable" and assessment.get("ban_strike_id"):
                        appeal_sent = facebook_scan.appeal_waba(
                            page, business_id, waba_id, uid, assessment["ban_strike_id"], log=log
                        )
                        if appeal_sent:
                            assessment["state"] = "in_review"
                    wabas_out.append({
                        "waba_id": waba_id,
                        "name": w.get("name") or assessment.get("name") or "",
                        "business_id": business_id,
                        "state": assessment["state"],
                        "appeal_sent": appeal_sent,
                    })

            log(f"[SCAN {profile_id}] Concluído — {len(wabas_out)} WABA(s)")
            return _result("ok", wabas=wabas_out)

    except Exception as exc:
        import traceback as _tb
        log(f"[SCAN {profile_id}] Exceção no browser: {exc}")
        print(_tb.format_exc(), flush=True)
        return {"type": "scan_result", "cmd_id": cmd_id, "ok": False, "error": str(exc)[:500]}
    finally:
        try:
            _client.close_browser(profile_id)
        except Exception:
            pass
        _open_pids.discard(profile_id)


async def _handle_scan_profile(msg: dict, outbox: asyncio.Queue, log=print):
    result = await asyncio.to_thread(_execute_scan_profile_sync, msg, log)
    await outbox.put(json.dumps(result))


# ── Manual single-WABA re-appeal handler ────────────────────────────────────────

def _execute_appeal_waba_sync(msg: dict, log=print) -> dict:
    cmd_id      = msg.get("cmd_id")
    profile_id  = msg.get("profile_id", "")
    business_id = msg.get("business_id", "")
    waba_id     = msg.get("waba_id", "")

    if not profile_id or not business_id or not waba_id:
        return {"type": "appeal_result", "cmd_id": cmd_id, "ok": False,
                "error": "profile_id/business_id/waba_id ausente"}

    log(f"[APPEAL {profile_id}] Iniciando appeal manual waba={waba_id}")

    try:
        from services.adspower import connect_cdp_with_retry
        from playwright.sync_api import sync_playwright
        from services import facebook_scan
    except Exception as exc:
        return {"type": "appeal_result", "cmd_id": cmd_id, "ok": False, "error": f"Falha ao importar dependências: {exc}"}

    try:
        browser_data = _client.open_browser(profile_id)
        _open_pids.add(profile_id)
    except Exception as exc:
        return {"type": "appeal_result", "cmd_id": cmd_id, "ok": False, "error": f"Falha ao abrir perfil AdsPower: {exc}"}

    ws_endpoint = (browser_data.get("ws") or {}).get("puppeteer", "")
    if not ws_endpoint:
        try:
            _client.close_browser(profile_id)
        except Exception:
            pass
        return {"type": "appeal_result", "cmd_id": cmd_id, "ok": False, "error": "Sem WebSocket endpoint do AdsPower"}

    try:
        with sync_playwright() as p:
            browser, _ws = connect_cdp_with_retry(p, ws_endpoint, profile_id=profile_id, ads_client=_client)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            old_pages = list(ctx.pages)
            page = ctx.new_page()
            for pg in old_pages:
                try:
                    pg.close()
                except Exception:
                    pass
            page.goto("https://business.facebook.com/latest/settings/whatsapp_account",
                       wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1500)

            if facebook_scan.is_checkpoint_url(page.url):
                return {"type": "appeal_result", "cmd_id": cmd_id, "ok": False, "error": "Perfil em checkpoint"}
            if facebook_scan.is_login_url(page.url) or not facebook_scan.is_logged_in(page, log=log):
                return {"type": "appeal_result", "cmd_id": cmd_id, "ok": False, "error": "Perfil não logado"}

            uid = facebook_scan.get_uid(page, log=log)
            assessment = facebook_scan.assess_waba(page, business_id, waba_id, log=log)
            if assessment["state"] != "appealable" or not assessment.get("ban_strike_id"):
                return {
                    "type": "appeal_result", "cmd_id": cmd_id, "ok": False,
                    "error": f"WABA não está em estado apelável (state={assessment['state']})",
                }

            sent = facebook_scan.appeal_waba(page, business_id, waba_id, uid, assessment["ban_strike_id"], log=log)
            return {"type": "appeal_result", "cmd_id": cmd_id, "ok": True, "appeal_sent": sent}

    except Exception as exc:
        import traceback as _tb
        log(f"[APPEAL {profile_id}] Exceção no browser: {exc}")
        print(_tb.format_exc(), flush=True)
        return {"type": "appeal_result", "cmd_id": cmd_id, "ok": False, "error": str(exc)[:500]}
    finally:
        try:
            _client.close_browser(profile_id)
        except Exception:
            pass
        _open_pids.discard(profile_id)


async def _handle_appeal_waba(msg: dict, outbox: asyncio.Queue, log=print):
    result = await asyncio.to_thread(_execute_appeal_waba_sync, msg, log)
    await outbox.put(json.dumps(result))


# ── Open-browser handler ───────────────────────────────────────────────────────

async def _handle_open_browser(msg: dict, log=print):
    """Open an AdsPower profile locally (fire-and-forget — no reply frame)."""
    profile_id = msg.get("profile_id", "")
    if not profile_id:
        return
    try:
        await asyncio.to_thread(_client.open_browser, profile_id)
        _open_pids.add(profile_id)
        _inactive_count.pop(profile_id, None)
        log(f"[CMD] Browser aberto para {profile_id}")
    except Exception as e:
        log(f"[CMD] Erro ao abrir browser: {e}")


# ── Browser-status pinger ──────────────────────────────────────────────────────

async def _browser_status_pinger(outbox: asyncio.Queue, stop: asyncio.Event):
    while not stop.is_set():
        await asyncio.sleep(5)
        if stop.is_set():
            break
        if _open_pids:
            for pid in list(_open_pids):
                state = await asyncio.to_thread(_client.is_browser_active, pid)
                if state == "active":
                    _inactive_count.pop(pid, None)
                elif state == "inactive":
                    _inactive_count[pid] = _inactive_count.get(pid, 0) + 1
                    if _inactive_count[pid] >= _INACTIVE_THRESHOLD:
                        _open_pids.discard(pid)
                        _inactive_count.pop(pid, None)
        await outbox.put(json.dumps({
            "type": "browser_status",
            "open_profile_ids": list(_open_pids),
        }))


# ── Receive / send loops ───────────────────────────────────────────────────────

async def _receiver(ws, outbox: asyncio.Queue, log=print):
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        t = msg.get("type")
        if t == "add_card":
            asyncio.create_task(_handle_add_card(msg, outbox, log))
        elif t == "open_browser":
            asyncio.create_task(_handle_open_browser(msg, log))
        elif t == "link_waba":
            asyncio.create_task(_handle_link_waba(msg, outbox, log))
        elif t == "list_profiles":
            asyncio.create_task(_handle_list_profiles(msg, outbox, log))
        elif t == "scan_profile":
            asyncio.create_task(_handle_scan_profile(msg, outbox, log))
        elif t == "appeal_waba":
            asyncio.create_task(_handle_appeal_waba(msg, outbox, log))
        # ping / unknown types are ignored — no action needed


async def _sender(ws, outbox: asyncio.Queue):
    while True:
        msg = await outbox.get()
        if msg is None:
            break
        await ws.send(msg)


# ── Connection loop with reconnect ─────────────────────────────────────────────

async def connect_loop(
    ws_url: str,
    *,
    log=print,
    on_status=None,   # callable(state: str) | None
    stop: asyncio.Event | None = None,
):
    """Maintain a single persistent WebSocket connection with auto-reconnect."""
    backoff = 5.0
    last_failure_repr = ""

    def _status(state: str):
        if on_status:
            on_status(state)

    while True:
        if stop and stop.is_set():
            break
        outbox: asyncio.Queue = asyncio.Queue()
        try:
            if last_failure_repr == "":
                log("[AGENT] Conectando…")
            _status("connecting")
            async with websockets.connect(
                ws_url,
                ping_interval=30,
                ping_timeout=10,
                open_timeout=15,
                compression=None,
            ) as ws:
                log("[AGENT] Conectado!")
                last_failure_repr = ""
                _status("online")
                backoff = 5.0

                stop_ev = asyncio.Event()
                sender_task = asyncio.create_task(_sender(ws, outbox))
                recv_task   = asyncio.create_task(_receiver(ws, outbox, log))
                pinger_task = asyncio.create_task(_browser_status_pinger(outbox, stop_ev))
                sync_task   = asyncio.create_task(_profile_sync_pinger(outbox, stop_ev, log))

                if stop:
                    while not stop.is_set():
                        if recv_task.done():
                            break
                        await asyncio.sleep(0.5)
                else:
                    await recv_task

                stop_ev.set()
                recv_task.cancel()
                pinger_task.cancel()
                sync_task.cancel()
                await outbox.put(None)
                await sender_task

        except (
            websockets.exceptions.ConnectionClosed,
            websockets.exceptions.InvalidHandshake,
            OSError,
            asyncio.TimeoutError,
        ) as e:
            repr_key = f"{type(e).__name__}: {str(e)[:120]}"
            if repr_key != last_failure_repr:
                log(f"[AGENT] Desconectado: {e}. Reconectando em {backoff:.0f}s…")
                last_failure_repr = repr_key
        except Exception as e:
            repr_key = f"{type(e).__name__}: {str(e)[:120]}"
            if repr_key != last_failure_repr:
                log(f"[AGENT] Erro inesperado: {e}. Reconectando em {backoff:.0f}s…")
                last_failure_repr = repr_key
        finally:
            _status("offline")

        if stop and stop.is_set():
            break

        log(f"[AGENT] Reconectando em {backoff:.0f}s…")
        for _ in range(int(backoff * 10)):
            if stop and stop.is_set():
                break
            await asyncio.sleep(0.1)
        backoff = min(backoff * 1.5, 60.0)

    log("[AGENT] Encerrado.")
    _status("offline")
