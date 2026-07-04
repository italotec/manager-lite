"""
disparo_multi.py — Multi-BM bulk send orchestrator.

Lists are transient in Lite: the caller (disparar.py routes) parses the upload
in memory and hands `start_batch`/`start_travar_broadcast` an already-deduped
list of row dicts — nothing here touches disk for the lead list itself.

Flow:
  1. allocate()              : split the full pool evenly across all selected
                               BMs — every lead is assigned, none dropped
  2. start_batch()            : fire one DisparoJob child per BM, each
                               getting a disjoint slice of the pool (reuses the
                               existing engine with preloaded_rows)
  3. start_travar_broadcast() : fire one DisparoJob child per selected BM, each
                               getting the FULL row list (no slicing)
  4. batch_status()           : aggregate child states into one summary
  5. batch_stop()              : propagate stop to all live children
"""

import json
import os
import threading
import uuid

from .disparar_service import (
    start_disparo_job,
    get_live_state,
    request_stop,
    count_sent_from_log,
    stamp_disparo_events,
)

GLOBAL_BATCH_BUDGET = 600   # max total thread-workers across all children (thread mode)
GLOBAL_ASYNC_BUDGET = 500   # max total async coroutines across all children (MAX mode)


# ── in-memory batch registry ───────────────────────────────────────────────────
# {batch_id: {user_id, children:[{job_id, waba_id, name, quota}], pool_size}}
_live_batches: dict[str, dict] = {}
_BATCH_LOCK = threading.Lock()


def _batch_sidecar_path(user_id: int, batch_id: str) -> str:
    base = os.path.join(os.getcwd(), "instance", "users", str(user_id))
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"batch_{batch_id}.json")


def _save_batch_sidecar(user_id: int, batch_id: str, data: dict) -> None:
    path = _batch_sidecar_path(user_id, batch_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _load_batch_sidecar(user_id: int, batch_id: str) -> dict | None:
    path = _batch_sidecar_path(user_id, batch_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ── allocator ──────────────────────────────────────────────────────────────────

def allocate(wabas_spec: list, pool_size: int) -> dict:
    """
    wabas_spec: list (in selection order) of:
      {waba_id, name, phone_number_id, token}

    Splits the full pool EVENLY across all selected BMs — every lead is
    assigned to exactly one BM, nothing is ever dropped. The first
    `pool_size % n` BMs get one extra row so the split is as even as integer
    division allows.

    Returns:
      {
        assignments: [{waba_id, name, phone_number_id, token, quota, start, end}],
        pool_size: int,
        error: str | None   # "insufficient_leads" when the pool is empty
      }
    """
    n = len(wabas_spec)
    if pool_size == 0 or n == 0:
        return {"assignments": [], "pool_size": pool_size, "error": "insufficient_leads"}

    base = pool_size // n
    extra = pool_size % n

    assignments = []
    cumulative = 0
    for i, spec in enumerate(wabas_spec):
        quota = base + (1 if i < extra else 0)
        if quota == 0:
            continue
        assignments.append({**spec, "quota": quota, "start": cumulative, "end": cumulative + quota})
        cumulative += quota

    return {
        "assignments": assignments,
        "pool_size": pool_size,
        "error": None,
    }


# ── batch start ────────────────────────────────────────────────────────────────

def start_batch(app, user_id: int,
                wabas_spec: list,
                pool: list,                 # pre-built, in-memory rows (original columns)
                phone_col: str,             # column in `pool` rows holding the phone number
                template_mode: str,         # "same" | "different"
                templates_cfg: dict | list, # same→dict, different→list[{waba_id,...}]
                max_workers: int,
                skip_log: bool) -> dict:
    """
    Lists in Lite are transient (parsed in memory, never written to csvs_dir), so
    the pool is built by the caller and handed in directly — no build_pool() call.

    Returns:
      {batch_id, children, pool_size, error}
    On allocation error: {error: "insufficient_leads"}
    """
    alloc = allocate(wabas_spec, len(pool))
    if alloc["error"]:
        return alloc

    # Compute per-child concurrency within global budget
    n_children = len(alloc["assignments"])
    if max_workers == 0:
        # MAX mode: children use async aiohttp; split GLOBAL_ASYNC_BUDGET across them
        child_workers = 0
        child_async_limit = max(1, GLOBAL_ASYNC_BUDGET // n_children)
    else:
        child_workers = max(1, min(max_workers, GLOBAL_BATCH_BUDGET // max(1, n_children)))
        child_async_limit = 500  # unused in thread mode

    batch_id = str(uuid.uuid4())[:8]
    children = []

    for bm in alloc["assignments"]:
        waba_id = bm["waba_id"]
        phone_number_id = bm["phone_number_id"]
        token = bm.get("token", "")

        # Resolve template + param_map for this BM
        if template_mode == "same":
            # Shared param_map; per-BM name/language (names vary across BMs)
            param_map = templates_cfg.get("param_map", [])
            bm_cfg = (templates_cfg.get("per_bm") or {}).get(waba_id, {})
            template_name = bm_cfg.get("template_name", "")
            template_language = bm_cfg.get("template_language", "en")
        else:
            # templates_cfg is a list; find entry for this waba_id
            cfg = next((c for c in templates_cfg if c.get("waba_id") == waba_id), {})
            template_name = cfg.get("template_name", "")
            template_language = cfg.get("template_language", "en")
            param_map = cfg.get("param_map", [])

        rows_slice = pool[bm["start"]:bm["end"]]

        job_id = start_disparo_job(
            app=app,
            user_id=user_id,
            csv_filename=f"batch_{batch_id}_{waba_id}",
            phone_col=phone_col,
            phone_number_id=phone_number_id,
            token=token,
            template_name=template_name,
            template_language=template_language,
            param_map=param_map,
            max_workers=child_workers,
            skip_log=skip_log,
            waba_id=waba_id,
            has_header=True,
            max_leads=0,
            preloaded_rows=rows_slice,
            async_limit=child_async_limit,
        )

        children.append({
            "job_id": job_id,
            "waba_id": waba_id,
            "name": bm.get("name", waba_id),
            "quota": bm["quota"],
            "workers": child_workers,
        })

    batch_data = {
        "user_id": user_id,
        "children": children,
        "pool_size": alloc["pool_size"],
        "requested_workers": max_workers,
        "effective_workers": child_workers,
    }

    with _BATCH_LOCK:
        _live_batches[batch_id] = batch_data

    _save_batch_sidecar(user_id, batch_id, batch_data)

    return {
        "batch_id": batch_id,
        "children": children,
        "pool_size": alloc["pool_size"],
        "error": None,
    }


# ── travar broadcast ──────────────────────────────────────────────────────────

def start_travar_broadcast(app, user_id: int,
                           wabas_resolved: list,
                           rows: list,
                           phone_col: str,
                           param_map: list,
                           max_workers: int,
                           skip_log: bool = True) -> dict:
    """
    Broadcast the same full `rows` list once to every WABA in `wabas_resolved`.
    Templates are pre-resolved (auto-picked APPROVED) by the caller.

    wabas_resolved: [{waba_id, name, phone_number_id, token,
                      template_name, template_language}, ...]
    rows: raw list dicts keyed by real column names (from the transient upload parse)
    skip_log=True: don't filter/write sent_log (same leads go to all WABAs)

    Returns {batch_id, children, pool_size, error: None}
    """
    n_children = len(wabas_resolved)
    if n_children == 0:
        return {"error": "no_valid_wabas"}

    if max_workers == 0:
        child_workers = 0
        child_async_limit = max(1, GLOBAL_ASYNC_BUDGET // n_children)
    else:
        child_workers = max(1, min(max_workers, GLOBAL_BATCH_BUDGET // n_children))
        child_async_limit = 500

    batch_id = str(uuid.uuid4())[:8]
    children = []

    for waba in wabas_resolved:
        waba_id = waba["waba_id"]
        job_id = start_disparo_job(
            app=app,
            user_id=user_id,
            csv_filename=f"travar_{batch_id}_{waba_id}",
            phone_col=phone_col,
            phone_number_id=waba["phone_number_id"],
            token=waba["token"],
            template_name=waba["template_name"],
            template_language=waba["template_language"],
            param_map=param_map,
            max_workers=child_workers,
            skip_log=skip_log,
            waba_id=waba_id,
            has_header=True,
            max_leads=0,
            preloaded_rows=rows,
            async_limit=child_async_limit,
        )
        children.append({
            "job_id": job_id,
            "waba_id": waba_id,
            "name": waba.get("name", waba_id),
            "quota": len(rows),
            "template": waba["template_name"],
            "workers": child_workers,
        })

    batch_data = {
        "user_id": user_id,
        "children": children,
        "pool_size": len(rows),
        "requested_workers": max_workers,
        "effective_workers": child_workers,
    }

    with _BATCH_LOCK:
        _live_batches[batch_id] = batch_data

    _save_batch_sidecar(user_id, batch_id, batch_data)

    return {
        "batch_id": batch_id,
        "children": children,
        "pool_size": len(rows),
        "error": None,
    }


# ── batch status ───────────────────────────────────────────────────────────────

def batch_status(user_id: int, batch_id: str) -> dict | None:
    """Aggregate child job states. Returns None if batch not found."""
    data = _live_batches.get(batch_id) or _load_batch_sidecar(user_id, batch_id)
    if not data:
        return None

    from .. import db
    from ..models import DisparoJob
    import flask

    totals = {"total": 0, "sent": 0, "failed": 0, "skipped": 0}
    children_out = []
    any_running = False
    any_error = False

    for child in data.get("children", []):
        job_id = child["job_id"]
        live = get_live_state(job_id)
        if live:
            st = live
            any_running = any_running or live["status"] == "running"
            any_error = any_error or live["status"] == "error"
        else:
            # Try DB
            st = None
            try:
                import datetime as _dt
                with flask.current_app.app_context():
                    job = db.session.get(DisparoJob, job_id)
                    if job:
                        # Self-heal: DB says still active but thread is gone — mark stopped.
                        # Grace of 60s avoids the race between DB insert and thread registration.
                        if job.status in ("queued", "running"):
                            age = (_dt.datetime.utcnow() - job.created_at).total_seconds()
                            if age > 60:
                                try:
                                    if getattr(job, "waba_id", "") and not getattr(job, "skip_log", False):
                                        recovered = count_sent_from_log(job.user_id, job.id)
                                        if recovered > 0:
                                            stamp_disparo_events(job.user_id, job.waba_id, recovered)
                                            job.sent = recovered
                                    job.status = "stopped"
                                    job.last_message = "Interrompido: processo encerrado."
                                    db.session.commit()
                                except Exception:
                                    try:
                                        db.session.rollback()
                                    except Exception:
                                        pass
                        st = {
                            "status": job.status,
                            "total": job.total,
                            "sent": job.sent,
                            "failed": job.failed,
                            "skipped": job.skipped,
                            "last_message": job.last_message,
                        }
                        if job.status == "error":
                            any_error = True
            except RuntimeError:
                pass
            if st is None:
                st = {"status": "unknown", "total": 0, "sent": 0,
                      "failed": 0, "skipped": 0, "last_message": ""}

        for k in ("total", "sent", "failed", "skipped"):
            totals[k] += st.get(k, 0)

        last_message = st.get("last_message", "") or ""
        erro_generic = bool(st.get("erro_generic_marked")) or "#135000" in last_message or "ERRO GENERIC" in last_message

        children_out.append({
            "job_id": job_id,
            "waba_id": child["waba_id"],
            "name": child["name"],
            "quota": child["quota"],
            "template": child.get("template", ""),
            "workers": child.get("workers", 0),
            "status": st.get("status", "unknown"),
            "sent": st.get("sent", 0),
            "failed": st.get("failed", 0),
            "skipped": st.get("skipped", 0),
            "total": st.get("total", 0),
            "last_message": last_message,
            "erro_generic": erro_generic,
        })

    processed = totals["sent"] + totals["failed"]
    countable = max(1, totals["total"] - totals["skipped"])
    pct = round(processed / countable * 100)

    if any_running:
        overall_status = "running"
    elif any_error:
        overall_status = "error"
    elif all(c["status"] in ("done", "stopped", "error") for c in children_out):
        overall_status = "done"
    else:
        overall_status = "running"

    locked = sum(1 for c in children_out if c["erro_generic"])

    return {
        "batch_id": batch_id,
        "status": overall_status,
        "pool_size": data.get("pool_size", 0),
        "pct": pct,
        "requested_workers": data.get("requested_workers"),
        "effective_workers": data.get("effective_workers"),
        "locked": locked,
        **totals,
        "children": children_out,
    }


# ── batch stop ─────────────────────────────────────────────────────────────────

def batch_stop(user_id: int, batch_id: str) -> bool:
    data = _live_batches.get(batch_id) or _load_batch_sidecar(user_id, batch_id)
    if not data:
        return False
    for child in data.get("children", []):
        request_stop(child["job_id"])
    return True
