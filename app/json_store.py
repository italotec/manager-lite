import os
import json
import time
import threading
import tempfile
from typing import Dict, Any

if os.name != "nt":
    import fcntl
else:
    fcntl = None

# Serializes read-modify-write on bms.json across threads. Without it, concurrent
# disparo jobs (multi-BM batch) clobber each other's snapshot updates and can
# corrupt the file. On POSIX we additionally flock() across processes; Lite runs
# single-process on Windows so the RLock alone is sufficient there.
_WRITE_LOCK = threading.RLock()

def user_dir(user_id: int) -> str:
    base = os.path.join(os.getcwd(), "instance", "users", str(user_id))
    os.makedirs(base, exist_ok=True)
    return base

def photos_dir(user_id: int) -> str:
    path = os.path.join(user_dir(user_id), "photos")
    os.makedirs(path, exist_ok=True)
    return path

def bms_path(user_id: int) -> str:
    return os.path.join(user_dir(user_id), "bms.json")

def ensure_user_bms_file(user_id: int) -> str:
    path = bms_path(user_id)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=4, ensure_ascii=False)
    return path

def load_user_bms(user_id: int) -> Dict[str, Any]:
    path = ensure_user_bms_file(user_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}

def save_user_bms(user_id: int, data: Dict[str, Any]) -> None:
    path = ensure_user_bms_file(user_id)
    dir_name = os.path.dirname(path)
    lock_path = path + ".lock"
    lock_file = open(lock_path, "w")
    try:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            # mkstemp gives each writer its own unique temp file so concurrent
            # writes never interleave into a shared .tmp file.
            fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()

def upsert_waba(user_id: int, waba_id: str, token: str, adspower_profile_id: str = "",
                business_manager_id: str = "", payment_account_id: str = "",
                serial_number: str = "") -> None:
    with _WRITE_LOCK:
        data = load_user_bms(user_id)
        key = str(waba_id).strip()
        if not key:
            return

        entry = data.get(key, {}) if isinstance(data.get(key), dict) else {}
        entry["waba_id"] = key
        entry["token"] = token
        entry["adspower_profile_id"] = adspower_profile_id or entry.get("adspower_profile_id", "")
        entry["business_manager_id"] = business_manager_id or entry.get("business_manager_id", "")
        entry["payment_account_id"] = payment_account_id or entry.get("payment_account_id", "")
        entry["serial_number"] = serial_number or entry.get("serial_number", "")
        entry.setdefault("phone_number_id", "")
        entry.setdefault("templates", [])
        entry.setdefault("origin", "lite")

        snap = entry.get("snapshot", {}) if isinstance(entry.get("snapshot"), dict) else {}
        snap.setdefault("waba_name", "")
        snap.setdefault("phone_numbers", [])
        snap.setdefault("template_counts", {"APPROVED": 0, "PAUSED": 0, "DISABLED": 0, "OTHER": 0})
        snap.setdefault("last_sync_at", 0)
        snap.setdefault("last_error", "")

        entry["snapshot"] = snap
        data[key] = entry
        save_user_bms(user_id, data)

def update_waba(user_id: int, old_waba_id: str, new_waba_id: str, token: str,
                adspower_profile_id: str = "", serial_number: str = "") -> tuple:
    """Edit an existing WABA in place, re-keying if the WABA ID changed.

    Returns (ok, err). Preserves snapshot/remarks/templates/origin."""
    with _WRITE_LOCK:
        data = load_user_bms(user_id)
        old_key = str(old_waba_id).strip()
        new_key = str(new_waba_id).strip()

        if old_key not in data or not isinstance(data.get(old_key), dict):
            return False, "WABA não encontrada."
        if new_key != old_key and new_key in data:
            return False, "Já existe uma WABA com esse ID."

        entry = data[old_key]
        entry["waba_id"] = new_key
        entry["token"] = token
        entry["adspower_profile_id"] = adspower_profile_id
        entry["serial_number"] = serial_number

        if new_key != old_key:
            del data[old_key]
        data[new_key] = entry
        save_user_bms(user_id, data)
        return True, None

def upsert_waba_full(user_id: int, entry: Dict[str, Any]) -> None:
    """Replica write: merge a whitelisted full WABA entry (incl. snapshot) from Manager."""
    key = str(entry.get("waba_id") or "").strip()
    if not key:
        return
    with _WRITE_LOCK:
        data = load_user_bms(user_id)
        cur = data.get(key, {}) if isinstance(data.get(key), dict) else {}
        for f in ("waba_id", "token", "phone_number_id", "adspower_profile_id",
                  "business_manager_id", "payment_account_id", "remarks",
                  "serial_number"):
            if f in entry:
                cur[f] = entry[f]
        if isinstance(entry.get("snapshot"), dict):
            cur["snapshot"] = entry["snapshot"]
        cur.setdefault("templates", [])
        cur["origin"] = "manager"
        data[key] = cur
        save_user_bms(user_id, data)


def replace_user_wabas(user_id: int, waba_ids_to_keep: set) -> None:
    """Prune WABAs no longer present in Manager (keeps Lite a faithful replica).

    WABAs added directly in Lite (origin == "lite") are never pruned here —
    they haven't been adopted by Manager yet, so Manager's payload can't know
    about them. They only become prunable once Manager echoes them back
    (upsert_waba_full flips their origin to "manager").
    """
    with _WRITE_LOCK:
        data = load_user_bms(user_id)
        removed = [
            k for k, v in data.items()
            if k not in waba_ids_to_keep
            and (not isinstance(v, dict) or v.get("origin") != "lite")
        ]
        for k in removed:
            del data[k]
        if removed:
            save_user_bms(user_id, data)


def get_lite_origin_wabas(user_id: int) -> list:
    """Return whitelisted fields for WABAs added in Lite and not yet adopted by Manager."""
    data = load_user_bms(user_id)
    fields = ("waba_id", "token", "phone_number_id", "adspower_profile_id",
              "business_manager_id", "payment_account_id", "serial_number", "remarks")
    out = []
    for v in data.values():
        if isinstance(v, dict) and v.get("origin") == "lite":
            out.append({f: v[f] for f in fields if f in v})
    return out


# Config fields compared/replaced when merging an existing WABA on import.
# Matching is by waba_id (the dict key); the live Meta `snapshot` is never touched.
MERGE_FIELDS = ("token", "adspower_profile_id", "serial_number")


def import_wabas(user_id: int, incoming: dict) -> dict:
    """Merge WABAs from an uploaded bms.json-shaped dict into the user's store.

    - WABA (by waba_id) not present     -> add the full entry (stamped origin="lite"
      so it replicates up to Manager and isn't pruned by replace_user_wabas).
    - present and nothing new           -> skip (flagged back to the caller).
    - present but a MERGE_FIELDS value differs (and the incoming value is
      non-empty) -> replace only that value; blanks never overwrite.

    Returns a report dict: {added:[wid], updated:[{waba_id, fields}], skipped:[wid], errors:[key]}.
    """
    report = {"added": [], "updated": [], "skipped": [], "errors": []}
    if not isinstance(incoming, dict):
        return report

    with _WRITE_LOCK:
        data = load_user_bms(user_id)
        for raw_key, entry in incoming.items():
            if not isinstance(entry, dict):
                report["errors"].append(str(raw_key))
                continue
            wid = str(entry.get("waba_id") or raw_key or "").strip()
            if not wid:
                report["errors"].append(str(raw_key))
                continue

            if wid not in data or not isinstance(data.get(wid), dict):
                # NEW → store the full incoming entry (defensive defaults).
                new_entry = dict(entry)
                new_entry["waba_id"] = wid
                new_entry.setdefault("phone_number_id", "")
                new_entry.setdefault("templates", [])
                new_entry.setdefault("snapshot", {})
                new_entry["origin"] = "lite"
                data[wid] = new_entry
                report["added"].append(wid)
            else:
                # EXISTING → field-level merge of config values only.
                cur = data[wid]
                changed = []
                for f in MERGE_FIELDS:
                    new_val = str(entry.get(f) or "").strip()
                    if new_val and new_val != str(cur.get(f) or "").strip():
                        cur[f] = new_val
                        changed.append(f)
                if changed:
                    report["updated"].append({"waba_id": wid, "fields": changed})
                else:
                    report["skipped"].append(wid)

        if report["added"] or report["updated"]:
            save_user_bms(user_id, data)

    return report


def update_snapshot(user_id: int, waba_id: str, **fields) -> None:
    with _WRITE_LOCK:
        data = load_user_bms(user_id)
        key = str(waba_id).strip()
        if key not in data or not isinstance(data.get(key), dict):
            return

        entry = data[key]
        snap = entry.get("snapshot", {}) if isinstance(entry.get("snapshot"), dict) else {}

        for k, v in fields.items():
            snap[k] = v

        if "last_sync_at" not in fields:
            snap["last_sync_at"] = int(time.time())

        entry["snapshot"] = snap
        data[key] = entry
        save_user_bms(user_id, data)


def merge_sync_snapshots(user_id: int, updates: Dict[str, Dict[str, Any]]) -> None:
    """Merge sync-produced snapshot fields into the CURRENT on-disk bms.

    `updates` maps bms key -> the snapshot fields to set for that WABA. Re-reads
    under the write lock so WABAs added/edited/deleted (or snapshot fields like
    disparou_at set by other flows) while the sync was running are preserved.
    Keys no longer present on disk are skipped (never resurrected)."""
    if not updates:
        return
    with _WRITE_LOCK:
        data = load_user_bms(user_id)
        for key, fields in updates.items():
            entry = data.get(key)
            if not isinstance(entry, dict):
                continue  # WABA deleted / re-keyed during sync — don't resurrect
            snap = entry.get("snapshot", {}) if isinstance(entry.get("snapshot"), dict) else {}
            snap.update(fields)
            entry["snapshot"] = snap
        save_user_bms(user_id, data)


def delete_wabas(user_id: int, waba_ids: list) -> int:
    """Delete WABAs by waba_id or bms key, under the write lock. Returns count deleted."""
    with _WRITE_LOCK:
        data = load_user_bms(user_id)
        deleted = 0
        for key in list(data.keys()):
            entry = data.get(key)
            if isinstance(entry, dict):
                wid = str(entry.get("waba_id") or "").strip()
                if wid in waba_ids or key in waba_ids:
                    del data[key]
                    deleted += 1
        if deleted:
            save_user_bms(user_id, data)
        return deleted


def save_waba_remarks(user_id: int, waba_id: str, text: str) -> None:
    with _WRITE_LOCK:
        data = load_user_bms(user_id)
        key = str(waba_id).strip()
        if key in data and isinstance(data.get(key), dict):
            data[key]["remarks"] = text
            save_user_bms(user_id, data)


def patch_snapshot(user_id: int, waba_id: str, **fields) -> None:
    """Update specific snapshot fields without touching last_sync_at."""
    with _WRITE_LOCK:
        data = load_user_bms(user_id)
        key = str(waba_id).strip()
        if key not in data or not isinstance(data.get(key), dict):
            return

        entry = data[key]
        snap = entry.get("snapshot", {}) if isinstance(entry.get("snapshot"), dict) else {}

        for k, v in fields.items():
            snap[k] = v

        entry["snapshot"] = snap
        data[key] = entry
        save_user_bms(user_id, data)
