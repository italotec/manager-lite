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


def _resolve_business_ids(page, log=print, fallback_id: str = "") -> list:
    """Navigate to /select and return all business_ids the profile owns.

    Single-BM profiles: Meta redirects away from /select → extract id from URL.
    Multi-BM profiles: page stays on /select → collect anchor hrefs.
    Falls back to facebook_link.resolve_owning_business_id, then to fallback_id.
    """
    import re
    from services import facebook_link

    try:
        page.goto("https://business.facebook.com/select", timeout=30000,
                  wait_until="domcontentloaded")
        _wait_for_fb_token(page, timeout=20000)
    except Exception as exc:
        log(f"[BM-ENUM] Falha ao navegar para /select: {exc}")
        return [fallback_id] if fallback_id else []

    current_url = page.url or ""

    # Meta redirected → single BM
    if "business_home" in current_url or ("business_id=" in current_url and "/select" not in current_url):
        m = re.search(r"business_id=(\d+)", current_url)
        if m:
            bid = m.group(1)
            log(f"[BM-ENUM] 1 BM detectado (redirect) → {bid}")
            return [bid]

    # Still on /select → collect all anchors
    if "/select" in current_url:
        try:
            hrefs = page.eval_on_selector_all(
                'a[href*="business_id="]',
                "els => els.map(e => e.getAttribute('href'))",
            )
            seen = []
            for href in (hrefs or []):
                m = re.search(r"business_id=(\d+)", href or "")
                if not m:
                    continue
                bid = m.group(1)
                if bid == "0" or bid in seen:
                    continue
                seen.append(bid)
            if seen:
                log(f"[BM-ENUM] {len(seen)} BMs detectados na /select: {seen}")
                return seen
        except Exception as exc:
            log(f"[BM-ENUM] Falha ao extrair hrefs de /select: {exc}")

    # Fallback: live resolution via facebook_link
    try:
        bid = facebook_link.resolve_owning_business_id(page, log=log)
        if bid:
            log(f"[BM-ENUM] 1 BM via resolve_owning_business_id → {bid}")
            return [bid]
    except Exception:
        pass

    if fallback_id:
        log(f"[BM-ENUM] Usando fallback_id={fallback_id}")
        return [fallback_id]

    return []


def _execute_link_waba_sync(msg: dict, log=print, emit=None) -> dict:
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
    bsp_names        = msg.get("bsp_names") or []

    # All partners the user already has a token for (main + secondaries) —
    # maps business_id -> token so an already-shared WABA can be registered
    # without re-sharing. See facebook_link.detect_waba_partner.
    known_partner_tokens = {
        str(p["business_id"]): p["token"]
        for p in (msg.get("known_partners") or [])
        if p.get("business_id") and p.get("token")
    }

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

    # Counts for multi-BM summary
    _n_ok = 0
    _n_err = 0
    _n_restrita = 0
    _n_awaiting = 0

    try:
        with sync_playwright() as p:
            browser, _ws = connect_cdp_with_retry(
                p, ws_endpoint,
                profile_id=profile_id,
                ads_client=_client,
            )
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()

            business_ids = _resolve_business_ids(page, log, fallback_id=business_id)
            if not business_ids:
                return _result("error", "Não foi possível resolver nenhum business_id do perfil")

            total = len(business_ids)
            log(f"[LINK {profile_id}] {total} BM(s) encontrado(s): {business_ids}")

            for idx, bid in enumerate(business_ids, start=1):
                cur_waba_id = ""
                try:
                    page.goto(
                        f"https://business.facebook.com/latest/settings/whatsapp_account?business_id={bid}",
                        timeout=60000, wait_until="domcontentloaded",
                    )
                    _wait_for_fb_token(page)

                    # Use caller-supplied waba_id / waba_name only when there is exactly one BM
                    if total == 1 and waba_id:
                        cur_waba_id = waba_id
                    else:
                        expected = waba_name if total == 1 else None
                        cur_waba_id = facebook_link.extract_waba_id_graphql(
                            page, bid, expected_name=expected, log=log
                        )

                    if not cur_waba_id:
                        msg_err = f"Não foi possível identificar WABA do BM {bid}"
                        log(f"[LINK {profile_id}] [{idx}/{total}] {msg_err}")
                        _n_err += 1
                        if total == 1:
                            return _result("error", msg_err)
                        if emit:
                            emit({
                                "type": "link_done", "profile_id": profile_id,
                                "status": "error", "message": msg_err,
                                "waba_id": "", "business_id": bid,
                                "bm_index": idx, "bm_total": total,
                            })
                        continue

                    log(f"[LINK {profile_id}] [{idx}/{total}] waba_id={cur_waba_id} bid={bid}")

                    # Detect a partner already holding this WABA (ignoring BSPs)
                    # so we skip re-sharing and, if we already have a token for
                    # that partner, register straight away.
                    try:
                        partner_matches = facebook_link.detect_waba_partner(
                            page, bid, cur_waba_id, bsp_names, log=log
                        )
                    except Exception as exc:
                        log(f"[LINK {profile_id}] [{idx}/{total}] detect_waba_partner falhou: {exc}")
                        partner_matches = []

                    reg_token = meta_token
                    already_shared = False
                    known_match = next(
                        (m for m in partner_matches if m["business_id"] in known_partner_tokens), None
                    )

                    if known_match:
                        reg_token = known_partner_tokens[known_match["business_id"]]
                        already_shared = True
                        log(f"[LINK {profile_id}] [{idx}/{total}] WABA já compartilhada com parceiro conhecido "
                            f"{known_match['name']!r} ({known_match['business_id']}) — pulando compartilhamento")
                    elif partner_matches:
                        pending = partner_matches[0]
                        msg_info = f"Aguardando token do parceiro {pending['name']} ({pending['business_id']})"
                        log(f"[LINK {profile_id}] [{idx}/{total}] {msg_info}")
                        _n_awaiting += 1
                        if total == 1:
                            return _result(
                                "awaiting_token", msg_info,
                                pending_partner_business_id=pending["business_id"],
                                pending_partner_name=pending["name"],
                            )
                        if emit:
                            emit({
                                "type": "link_done", "profile_id": profile_id,
                                "status": "awaiting_token", "message": msg_info,
                                "waba_id": cur_waba_id, "business_id": bid,
                                "pending_partner_business_id": pending["business_id"],
                                "pending_partner_name": pending["name"],
                                "bm_index": idx, "bm_total": total,
                            })
                        continue

                    if not already_shared:
                        try:
                            ok = facebook_link.share_waba_graphql(page, bid, partner_biz_id, cur_waba_id, log=log)
                        except facebook_link.BmRestrictedException as exc:
                            log(f"[LINK {profile_id}] [{idx}/{total}] BM restrito: {exc}")
                            _n_restrita += 1
                            if total == 1:
                                return _result("restrita", str(exc))
                            if emit:
                                emit({
                                    "type": "link_done", "profile_id": profile_id,
                                    "status": "restrita", "message": str(exc),
                                    "waba_id": cur_waba_id, "business_id": bid,
                                    "bm_index": idx, "bm_total": total,
                                })
                            continue

                        if not ok:
                            msg_err = f"Falha ao compartilhar WABA {cur_waba_id} do BM {bid}"
                            log(f"[LINK {profile_id}] [{idx}/{total}] {msg_err}")
                            _n_err += 1
                            if total == 1:
                                return _result("error", msg_err)
                            if emit:
                                emit({
                                    "type": "link_done", "profile_id": profile_id,
                                    "status": "error", "message": msg_err,
                                    "waba_id": cur_waba_id, "business_id": bid,
                                    "bm_index": idx, "bm_total": total,
                                })
                            continue

                        log(f"[LINK {profile_id}] [{idx}/{total}] WABA compartilhada com partner={partner_biz_id}")

                    try:
                        serial_number = ""
                        try:
                            serial_number = str((_client.get_profile(profile_id) or {}).get("serial_number") or "")
                        except Exception:
                            pass

                        reg = register_business_manager(
                            base_url=manager_base_url,
                            api_key=manager_api_key,
                            waba_id=cur_waba_id,
                            token=reg_token,
                            adspower_profile_id=profile_id,
                            serial_number=serial_number,
                        )
                        if not reg["ok"]:
                            raise RuntimeError(reg.get("error") or "Manager API error")
                        log(f"[LINK {profile_id}] [{idx}/{total}] Registrado no Manager Lite")
                    except Exception as exc_reg:
                        msg_err = f"Falha ao registrar no Manager Lite: {exc_reg}"
                        log(f"[LINK {profile_id}] [{idx}/{total}] {msg_err}")
                        _n_err += 1
                        if total == 1:
                            return _result("error", msg_err)
                        if emit:
                            emit({
                                "type": "link_done", "profile_id": profile_id,
                                "status": "error", "message": msg_err,
                                "waba_id": cur_waba_id, "business_id": bid,
                                "bm_index": idx, "bm_total": total,
                            })
                        continue

                    _n_ok += 1
                    log(f"[LINK {profile_id}] [{idx}/{total}] ✓ BM {bid} vinculado")

                    if total == 1:
                        return {
                            "type": "link_done", "profile_id": profile_id,
                            "status": "ok", "message": "",
                            "waba_id": cur_waba_id, "business_id": bid,
                            "shared": not already_shared, "registered": True,
                        }

                    if emit:
                        emit({
                            "type": "link_done", "profile_id": profile_id,
                            "status": "ok", "message": "",
                            "waba_id": cur_waba_id, "business_id": bid,
                            "shared": not already_shared, "registered": True,
                            "bm_index": idx, "bm_total": total,
                        })

                except Exception as exc_bm:
                    import traceback as _tb
                    log(f"[LINK {profile_id}] [{idx}/{total}] Exceção no BM {bid}: {exc_bm}")
                    print(_tb.format_exc(), flush=True)
                    _n_err += 1
                    if total == 1:
                        return _result("error", str(exc_bm)[:500])
                    if emit:
                        emit({
                            "type": "link_done", "profile_id": profile_id,
                            "status": "error", "message": str(exc_bm)[:500],
                            "waba_id": cur_waba_id, "business_id": bid,
                            "bm_index": idx, "bm_total": total,
                        })

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

    # Multi-BM summary frame (single-BM returns early above)
    if _n_ok > 0:
        summary_status = "ok"
    elif _n_err == 0 and _n_restrita == 0 and _n_awaiting > 0:
        summary_status = "awaiting_token"
    else:
        summary_status = "error"
    log(f"[LINK {profile_id}] Resumo: ok={_n_ok} erro={_n_err} restrita={_n_restrita} aguardando={_n_awaiting}")
    return {
        "type": "link_summary",
        "profile_id": profile_id,
        "status": summary_status,
        "total": _n_ok + _n_err + _n_restrita + _n_awaiting,
        "ok": _n_ok,
        "failed": _n_err,
        "restrita": _n_restrita,
        "awaiting": _n_awaiting,
    }


async def _handle_link_waba(msg: dict, outbox: asyncio.Queue, log=print):
    profile_id = msg.get("profile_id")
    await outbox.put(json.dumps({"type": "link_start", "profile_id": profile_id}))
    loop = asyncio.get_event_loop()

    def emit(frame: dict):
        loop.call_soon_threadsafe(outbox.put_nowait, json.dumps(frame))

    async with _LINK_SEMAPHORE:
        result = await asyncio.to_thread(_execute_link_waba_sync, msg, log, emit)
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
