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
    if not business_id:
        return _result(False, error="WABA sem business_manager_id definido no Manager Lite")

    log(f"[CARD {profile_id}] Iniciando add_card (•••• {str(card.get('number',''))[-4:]})")

    try:
        from services.adspower import connect_cdp_with_retry
        from playwright.sync_api import sync_playwright
        from services import facebook_card
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
