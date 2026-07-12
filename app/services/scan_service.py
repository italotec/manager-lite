"""
scan_service.py — WABA health scanner job runner.

Job flow:
1. Ask the agent for the full list of AdsPower profiles (list_profiles).
2. Skip any profile already recorded in ScanProfile — resume support: a
   restarted scan picks up where it left off (unless this is a full rescan).
3. Scan profiles ONE AT A TIME (the operator's explicit choice — safest for
   the local machine), sending scan_profile and waiting for each reply
   before starting the next.
4. Immediately upsert ScanProfile + ScanWaba rows after each reply, so the
   dashboard updates in real time and a crash/restart loses no progress.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

_live_jobs: dict[int, dict] = {}
_jobs_lock = threading.Lock()
_job_counter = 0
_counter_lock = threading.Lock()


def _next_job_id() -> int:
    global _job_counter
    with _counter_lock:
        _job_counter += 1
        return _job_counter


def get_job(job_id: int) -> Optional[dict]:
    return _live_jobs.get(job_id)


def _run_job(app, job_id: int, user_id: int, auto_appeal: bool, rescan: bool):
    from .. import db
    from ..models import ScanProfile, ScanWaba, ScanProfileArchive, ScanWabaArchive
    from ..routes.agent_ws import send_command_and_wait

    state = _live_jobs[job_id]
    state["status"] = "running"

    with app.app_context():
        if rescan:
            # Archive the current rows before wiping them — "rescan all"
            # should never destroy data the operator can't get back.
            batch_at = datetime.utcnow()
            for p in ScanProfile.query.filter_by(user_id=user_id).all():
                db.session.add(ScanProfileArchive(
                    user_id=user_id, batch_at=batch_at, profile_id=p.profile_id,
                    profile_name=p.profile_name, outcome=p.outcome, detail=p.detail,
                    scanned_at=p.scanned_at,
                ))
            for w in ScanWaba.query.filter_by(user_id=user_id).all():
                db.session.add(ScanWabaArchive(
                    user_id=user_id, batch_at=batch_at, waba_id=w.waba_id,
                    waba_name=w.waba_name, business_id=w.business_id,
                    profile_id=w.profile_id, profile_name=w.profile_name,
                    state=w.state, appeal_sent=w.appeal_sent, scanned_at=w.scanned_at,
                ))
            ScanProfile.query.filter_by(user_id=user_id).delete()
            ScanWaba.query.filter_by(user_id=user_id).delete()
            db.session.commit()

        listing = send_command_and_wait(user_id, {"type": "list_profiles"}, timeout=60.0)
        if not listing.get("ok"):
            state["status"] = "error"
            state["error"] = listing.get("error", "Falha ao listar perfis")
            return

        all_profiles = listing.get("profiles") or []
        already_scanned = {
            pid for (pid,) in db.session.query(ScanProfile.profile_id)
            .filter_by(user_id=user_id).all()
        }
        pending = [p for p in all_profiles if p["profile_id"] not in already_scanned]

        with _jobs_lock:
            state["total"] = len(all_profiles)
            state["done"] = len(already_scanned)

        for profile in pending:
            if state.get("stop_requested"):
                break

            profile_id = profile["profile_id"]
            profile_name = profile.get("name", "")

            res = send_command_and_wait(
                user_id,
                {"type": "scan_profile", "profile_id": profile_id, "auto_appeal": auto_appeal},
                timeout=180.0,
                stop_event=state["stop_event"],
            )

            if res.get("stopped"):
                # Interrupted mid-wait by request_stop — don't record a fake
                # result for a profile the agent may still be processing.
                break

            if not res.get("ok"):
                outcome = "error"
                detail = res.get("error", "Falha desconhecida")
                wabas = []
            else:
                outcome = res.get("state", "error")
                detail = res.get("detail", "")
                wabas = res.get("wabas") or []

            sp = db.session.get(ScanProfile, profile_id)
            if sp is None:
                sp = ScanProfile(profile_id=profile_id, user_id=user_id)
                db.session.add(sp)
            sp.user_id = user_id
            sp.profile_name = profile_name
            sp.outcome = outcome
            sp.detail = detail
            sp.scanned_at = datetime.utcnow()

            for w in wabas:
                waba_id = w["waba_id"]
                sw = ScanWaba.query.filter_by(user_id=user_id, waba_id=waba_id).first()
                if sw is None:
                    sw = ScanWaba(user_id=user_id, waba_id=waba_id)
                    db.session.add(sw)
                sw.waba_name = w.get("name", "")
                sw.business_id = w.get("business_id", "")
                sw.profile_id = profile_id
                sw.profile_name = profile_name
                sw.state = w.get("state", "")
                sw.appeal_sent = bool(w.get("appeal_sent"))
                sw.scanned_at = datetime.utcnow()

            db.session.commit()

            with _jobs_lock:
                state["done"] += 1
                if outcome == "ok":
                    state["success"] += 1
                else:
                    state["failed"] += 1
                state["results"].append({
                    "profile_id": profile_id,
                    "profile_name": profile_name,
                    "outcome": outcome,
                    "waba_count": len(wabas),
                })

        state["status"] = "stopped" if state.get("stop_requested") else "done"


def start_scan_job(user_id: int, auto_appeal: bool = True, rescan: bool = False) -> int:
    """Launch the background scan job. Returns job_id."""
    from flask import current_app

    job_id = _next_job_id()
    state = {
        "job_id": job_id,
        "status": "queued",
        "total": 0,
        "done": 0,
        "success": 0,
        "failed": 0,
        "results": [],
        "stop_requested": False,
        "stop_event": threading.Event(),
    }
    with _jobs_lock:
        _live_jobs[job_id] = state

    app = current_app._get_current_object()
    t = threading.Thread(
        target=_run_job,
        args=(app, job_id, user_id, auto_appeal, rescan),
        daemon=True,
    )
    t.start()
    return job_id


def request_stop(job_id: int) -> bool:
    """Signal the job to stop. Returns whether a live job was found."""
    with _jobs_lock:
        state = _live_jobs.get(job_id)
        if not state:
            return False
        state["stop_requested"] = True
        state["stop_event"].set()
        return True
