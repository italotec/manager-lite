import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import (
    Blueprint, request,
    jsonify, current_app,
)
from flask_login import login_required, current_user

from .. import db
from ..models import DisparoJob
from ..json_store import load_user_bms
from ..services.meta import get_templates as meta_get_templates, _count_body_vars as _count_tpl_vars, pick_test_template
from ..config import Config
from ..services.disparar_service import (
    start_disparo_job,
    disparo_log_path,
    get_live_state,
    request_stop,
)
from ..services.disparo_multi import (
    start_batch,
    start_travar_broadcast,
    batch_status as _batch_status,
    batch_stop as _batch_stop,
)
from ..services.list_parser import allowed_list_file, parse_uploaded_list, dedup_by_phone
from ..services.list_cache import store as list_store, get as list_get

bp = Blueprint("disparar", __name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _wabas_with_phones(user_id: int) -> list:
    """Return WABAs that have at least one phone number in the snapshot,
    ready to use as sender options."""
    bms = load_user_bms(user_id)
    result = []
    for waba_id, data in bms.items():
        if not isinstance(data, dict):
            continue
        snap = data.get("snapshot", {}) or {}
        token = data.get("token", "")
        phone_numbers = snap.get("phone_numbers", []) or []

        phones = []
        for p in phone_numbers:
            pid = p.get("id", "")
            display = p.get("display_phone_number", pid)
            if pid:
                phones.append({"phone_number_id": pid, "display": display})

        reg = data.get("phone_number_id", "")
        if reg and not any(ph["phone_number_id"] == reg for ph in phones):
            phones.append({"phone_number_id": reg, "display": reg})

        tier = snap.get("messaging_limit_tier") or ""
        health_ok_at = snap.get("health_test_ok_at") or 0
        health_ok = bool(health_ok_at) and (time.time() - health_ok_at) < 86400
        # Prefer a CONNECTED Brazilian (+55) number; fall back to the first sender.
        preferred = _pick_connected_br_phone(phone_numbers) or (phones[0]["phone_number_id"] if phones else "")
        result.append({
            "waba_id": waba_id,
            "name": snap.get("waba_name") or waba_id,
            "token": token,
            "phones": phones,
            "preferred_phone_id": preferred,
            "tier": tier,
            "health_ok": health_ok,
        })
    return result


def _pick_connected_br_phone(phone_numbers: list) -> str:
    """Return the id of the first CONNECTED Brazilian (+55) number, or ''."""
    import re
    for p in phone_numbers:
        digits = re.sub(r"\D", "", str(p.get("display_phone_number") or ""))
        if digits.startswith("55") and (p.get("status") or "").upper() == "CONNECTED":
            return p.get("id", "") or ""
    return ""


@bp.route("/disparar/wabas")
@login_required
def disparar_wabas():
    return jsonify({"wabas": _wabas_with_phones(current_user.id)})


# ── transient list upload ───────────────────────────────────────────────────

@bp.route("/disparar/list/upload", methods=["POST"])
@login_required
def upload_list():
    f = request.files.get("list_file")
    if not f or not f.filename or not allowed_list_file(f.filename):
        return jsonify({"error": "Arquivo inválido. Envie um .csv ou .xlsx."}), 400

    has_header = request.form.get("has_header", "1") != "0"
    try:
        rows = parse_uploaded_list(f, has_header=has_header)
    except Exception as exc:
        return jsonify({"error": f"Erro ao ler arquivo: {exc}"}), 500

    if not rows:
        return jsonify({"error": "Arquivo vazio ou sem linhas de dados."}), 400

    columns = list(rows[0].keys())
    list_id = list_store(current_user.id, rows, columns)

    return jsonify({
        "list_id": list_id,
        "columns": columns,
        "preview": rows[:3],
        "row_count": len(rows),
    })


# ── templates (from Meta API, with full components for preview) ──────────────

@bp.route("/disparar/waba/<waba_id>/templates")
@login_required
def waba_templates(waba_id):
    bms = load_user_bms(current_user.id)
    waba = bms.get(str(waba_id))
    if not waba or not isinstance(waba, dict):
        return jsonify({"error": "WABA não encontrada."}), 404

    token = waba.get("token", "")
    if not token:
        return jsonify({"error": "Token não encontrado para esta WABA."}), 400

    templates, err = meta_get_templates(Config.META_API_VERSION, token, waba_id)
    if err:
        return jsonify({"error": err}), 502

    auto = pick_test_template(templates)

    result = []
    for t in templates:
        result.append({
            "name":       t.get("name", ""),
            "category":   t.get("category", ""),
            "status":     t.get("status", ""),
            "language":   t.get("language", ""),
            "components": t.get("components", []),
            "var_count":  _count_tpl_vars(t),
        })

    return jsonify({
        "templates": result,
        "auto_template_name": (auto or {}).get("name", ""),
    })


# ── start single/multi-BM disparo ──────────────────────────────────────────────

@bp.route("/disparar/start", methods=["POST"])
@login_required
def start_disparo():
    """Fire to ONE selected BM."""
    data = request.get_json(silent=True) or {}

    list_id            = (data.get("list_id") or "").strip()
    phone_col          = (data.get("phone_col") or "").strip()
    phone_number_id    = (data.get("phone_number_id") or "").strip()
    token               = (data.get("token") or "").strip()
    template_name       = (data.get("template_name") or "").strip()
    template_language   = (data.get("template_language") or "en").strip()
    param_map           = data.get("param_map", [])
    waba_id             = (data.get("waba_id") or "").strip()
    _w = data.get("max_workers")
    max_workers = int(_w) if _w is not None else 10
    if max_workers != 0:
        max_workers = max(1, min(max_workers, 500))

    if not all([list_id, phone_col, phone_number_id, token, template_name]):
        return jsonify({"error": "Campos obrigatórios faltando."}), 400

    entry = list_get(current_user.id, list_id)
    if not entry:
        return jsonify({"error": "Lista expirada ou não encontrada — envie novamente."}), 404
    if phone_col not in entry["columns"]:
        return jsonify({"error": f"Coluna '{phone_col}' não encontrada na lista."}), 400

    job_id = start_disparo_job(
        app=current_app._get_current_object(),
        user_id=current_user.id,
        csv_filename=f"lite_{list_id}",
        phone_col=phone_col,
        phone_number_id=phone_number_id,
        token=token,
        template_name=template_name,
        template_language=template_language,
        param_map=param_map,
        max_workers=max_workers,
        skip_log=True,          # lists are transient — never dedup/log against sent_log
        waba_id=waba_id,
        has_header=True,
        preloaded_rows=entry["rows"],
    )
    return jsonify({"job_id": job_id})


@bp.route("/disparar/batch/start", methods=["POST"])
@login_required
def batch_start():
    """Fire to MULTIPLE selected BMs — the list is deduped and split evenly
    across them (each lead contacted once, 100% of the list is sent)."""
    data = request.get_json(silent=True) or {}

    list_id       = (data.get("list_id") or "").strip()
    phone_col     = (data.get("phone_col") or "").strip()
    wabas_spec    = data.get("wabas_spec", [])       # [{waba_id, name, phone_number_id, token, template_name, template_language, param_map}]
    _w = data.get("max_workers")
    max_workers = int(_w) if _w is not None else 10
    if max_workers != 0:
        max_workers = max(1, min(max_workers, 500))

    if not list_id or not phone_col:
        return jsonify({"error": "Selecione uma lista e a coluna de telefone."}), 400
    if not wabas_spec:
        return jsonify({"error": "Selecione pelo menos um BM."}), 400

    entry = list_get(current_user.id, list_id)
    if not entry:
        return jsonify({"error": "Lista expirada ou não encontrada — envie novamente."}), 404
    if phone_col not in entry["columns"]:
        return jsonify({"error": f"Coluna '{phone_col}' não encontrada na lista."}), 400

    pool = dedup_by_phone(entry["rows"], phone_col)

    templates_cfg = [
        {
            "waba_id": w.get("waba_id"),
            "template_name": w.get("template_name", ""),
            "template_language": w.get("template_language", "en"),
            "param_map": w.get("param_map", []),
        }
        for w in wabas_spec
    ]

    result = start_batch(
        app=current_app._get_current_object(),
        user_id=current_user.id,
        wabas_spec=wabas_spec,
        pool=pool,
        phone_col=phone_col,
        template_mode="different",
        templates_cfg=templates_cfg,
        max_workers=max_workers,
        skip_log=True,
    )

    if result.get("error") == "insufficient_leads":
        return jsonify({"error": "Lista vazia — envie uma lista com leads."}), 400
    if result.get("error"):
        return jsonify({"error": result["error"]}), 400

    return jsonify(result)


# ── travar (lock) — single broadcast pass to every selected BM ────────────────

@bp.route("/disparar/travar/start", methods=["POST"])
@login_required
def travar_start():
    data = request.get_json(silent=True) or {}
    waba_ids    = data.get("waba_ids") or []
    list_id     = (data.get("list_id") or "").strip()
    phone_col   = (data.get("phone_col") or "").strip()
    param_map   = data.get("param_map") or []
    _w = data.get("max_workers")
    max_workers = int(_w) if _w is not None else 10

    if not waba_ids or not list_id or not phone_col:
        return jsonify({"error": "Campos obrigatórios faltando."}), 400

    entry = list_get(current_user.id, list_id)
    if not entry:
        return jsonify({"error": "Lista expirada ou não encontrada — envie novamente."}), 404
    if phone_col not in entry["columns"]:
        return jsonify({"error": f"Coluna '{phone_col}' não encontrada na lista."}), 400

    api_version = current_app.config["META_API_VERSION"]
    bms = load_user_bms(current_user.id)

    def _resolve_waba(waba_id):
        entry_bm = bms.get(str(waba_id))
        if not isinstance(entry_bm, dict):
            return waba_id, None, f"{waba_id}: não encontrado"
        token = (entry_bm.get("token") or "").strip()
        snap = entry_bm.get("snapshot", {}) or {}
        phone_numbers = snap.get("phone_numbers") or []
        waba_name = snap.get("waba_name") or str(waba_id)
        phone_number_id = _pick_connected_br_phone(phone_numbers)
        if not token:
            return waba_id, None, f"{waba_id}: token vazio"
        if not phone_number_id:
            return waba_id, None, f"{waba_id}: sem número brasileiro (+55) conectado"
        try:
            templates, err_tpl = meta_get_templates(api_version, token, waba_id)
        except Exception as exc:
            return waba_id, None, f"{waba_id}: erro ao buscar templates — {exc}"
        if err_tpl or not templates:
            return waba_id, None, f"{waba_id}: {err_tpl or 'lista vazia'}"
        # Prefer an APPROVED UTILITY template; fall back to any APPROVED one.
        chosen = pick_test_template(templates)
        if not chosen:
            return waba_id, None, f"{waba_id}: nenhum template APPROVED disponível"
        return waba_id, {
            "waba_id": waba_id,
            "name": waba_name,
            "phone_number_id": phone_number_id,
            "token": token,
            "template_name": chosen.get("name", ""),
            "template_language": chosen.get("language", "pt"),
        }, None

    errors = []
    wabas_resolved = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_resolve_waba, wid): wid for wid in waba_ids}
        for future in as_completed(futures):
            try:
                waba_id, spec, err = future.result()
            except Exception as exc:
                wid = futures[future]
                errors.append(f"{wid}: erro interno — {exc}")
                continue
            if err:
                errors.append(err)
            else:
                wabas_resolved.append(spec)

    if not wabas_resolved:
        return jsonify({"batch_id": None, "children": [], "errors": errors}), 400

    result = start_travar_broadcast(
        app=current_app._get_current_object(),
        user_id=current_user.id,
        wabas_resolved=wabas_resolved,
        rows=entry["rows"],
        phone_col=phone_col,
        param_map=param_map,
        max_workers=max_workers,
        skip_log=True,
    )

    return jsonify({**result, "errors": errors})


# ── job status / logs / stop ──────────────────────────────────────────────────

@bp.route("/disparar/job/<int:job_id>/status")
@login_required
def job_status(job_id):
    live = get_live_state(job_id)
    if live:
        total, sent, failed, skipped = live["total"], live["sent"], live["failed"], live["skipped"]
        status, last_message = live["status"], live["last_message"]
        erro_generic_marked = bool(live.get("erro_generic_marked"))
    else:
        job = db.session.get(DisparoJob, job_id)
        if not job or job.user_id != current_user.id:
            return jsonify({"error": "not found"}), 404
        total, sent, failed, skipped = job.total, job.sent, job.failed, job.skipped
        status, last_message = job.status, job.last_message
        erro_generic_marked = False

    processed = sent + failed
    remaining = max(0, (total - skipped) - processed)
    pct = round(processed / max(1, total - skipped) * 100)
    erro_generic = erro_generic_marked or "#135000" in (last_message or "") or "ERRO GENERIC" in (last_message or "")

    return jsonify({
        "status": status, "total": total, "sent": sent, "failed": failed,
        "skipped": skipped, "remaining": remaining, "pct": pct,
        "last_message": last_message, "erro_generic": erro_generic,
    })


@bp.route("/disparar/job/<int:job_id>/logs")
@login_required
def job_logs(job_id):
    job = db.session.get(DisparoJob, job_id)
    if not job or job.user_id != current_user.id:
        return jsonify({"error": "not found"}), 404

    offset = int(request.args.get("offset", 0))
    log_path = disparo_log_path(current_user.id, job_id)

    entries = []
    new_offset = offset
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        slice_ = all_lines[offset:]
        new_offset = offset + len(slice_)
        for ln in slice_:
            try:
                entries.append(json.loads(ln.strip()))
            except Exception:
                pass

    return jsonify({"entries": entries, "new_offset": new_offset})


@bp.route("/disparar/job/<int:job_id>/stop", methods=["POST"])
@login_required
def stop_job(job_id):
    if request_stop(job_id):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "msg": "Job not running"})


# ── batch status / stop ───────────────────────────────────────────────────────

@bp.route("/disparar/batch/<batch_id>/status")
@login_required
def batch_status_route(batch_id):
    status = _batch_status(current_user.id, batch_id)
    if status is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(status)


@bp.route("/disparar/batch/<batch_id>/stop", methods=["POST"])
@login_required
def batch_stop_route(batch_id):
    ok = _batch_stop(current_user.id, batch_id)
    return jsonify({"ok": ok})
